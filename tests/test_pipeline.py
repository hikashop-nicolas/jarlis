"""Tests for pipeline.py: process_inbox routing without IMAP."""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from jarlis import imap_fetch, memory, pipeline
from jarlis.config import Config, init_paths
from jarlis.models import (
    ARCHIVE_IGNORED_TOPIC,
    ARCHIVE_SPAM,
    BUCKET_ARCHIVE,
    BUCKET_DRAFTED,
    BUCKET_FLAGGED,
)


def _make_cfg(tmp: Path) -> Config:
    cfg = Config()
    cfg.project_root = tmp
    init_paths(cfg)
    memory.ensure_layout(cfg)
    cfg.inbox_dir.mkdir(parents=True, exist_ok=True)
    cfg.attachments_dir.mkdir(parents=True, exist_ok=True)
    cfg.user.languages = ["en"]
    cfg.user.email = "you@you.tld"  # default test fixture's recipient, passes addressed filter
    cfg.pipeline.recipient_filter = "addressed"
    return cfg


def _build_eml(
    *,
    sender: str = "Alice <alice@example.com>",
    subject: str = "Hello",
    body: str = "hi there",
    message_id: str = "",
    in_reply_to: str = "",
) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "you@you.tld"
    msg["Subject"] = subject
    msg["Message-ID"] = message_id or f"<auto-{hash(subject) % 10**8}@x>"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    msg["Date"] = "Thu, 7 May 2026 10:00:00 +0000"
    msg.set_content(body)
    return bytes(msg)


def _drop_in_inbox(cfg: Config, raw: bytes) -> Path:
    """Reuse imap_fetch.write_email to materialize an inbox folder from raw RFC822."""
    folder, _ = imap_fetch.write_email(cfg, raw)
    return folder


class _StubBackend:
    name = "stub"

    def __init__(self, classifier_response: dict, draft_text: str = "Draft body") -> None:
        self.classifier_response = classifier_response
        self.draft_text = draft_text
        self.text_calls: list[str] = []
        self.json_calls: list[str] = []

    def call_text(self, prompt: str) -> str:
        self.text_calls.append(prompt)
        return self.draft_text

    def call_json(self, prompt: str) -> dict:
        self.json_calls.append(prompt)
        return self.classifier_response


# ---------- routing tests -------------------------------------------------


def test_draft_system_prompt_forbids_appending_footer() -> None:
    """The drafting instructions must explicitly tell the LLM not to add an
    organizational footer; voice exemplars typically contain one and the LLM
    will mimic it without explicit guidance, bloating the notification email."""
    assert "Do NOT append any organizational footer" in pipeline._DRAFT_SYSTEM
    assert "client appends the real footer at send time" in pipeline._DRAFT_SYSTEM


def test_generate_draft_does_not_append_footer_when_unset() -> None:
    """With ``[organization].footer`` empty (default), the draft is the raw
    backend output: no automatic footer addition."""
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        # cfg.organization.footer left at default ("")
        _drop_in_inbox(cfg, _build_eml(subject="Need input"))
        backend = _StubBackend(
            classifier_response={"bucket": "drafted", "archive_reason": None, "topic_slugs": [], "reason": "r"},
            draft_text="Hi Alice, here are my thoughts.\n\nThanks,\nAlice",
        )
        pipeline.process_inbox(cfg, backend=backend)
        draft = next(cfg.queue_dir.glob("*.md")).read_text(encoding="utf-8")
        assert "Hi Alice" in draft
        assert "Thanks,\nAlice" in draft
        # No footer added.
        assert "ACME" not in draft and "Address:" not in draft


def test_generate_draft_appends_footer_when_set() -> None:
    """When ``[organization].footer`` is set, it's appended verbatim after a
    blank line so the user can keep their workflow."""
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.organization.footer = "--\nAcme Parents Association\nteam@acme.example"
        _drop_in_inbox(cfg, _build_eml(subject="Need input"))
        backend = _StubBackend(
            classifier_response={"bucket": "drafted", "archive_reason": None, "topic_slugs": [], "reason": "r"},
            draft_text="Hi Alice, thanks.\n\nAlice",
        )
        pipeline.process_inbox(cfg, backend=backend)
        draft = next(cfg.queue_dir.glob("*.md")).read_text(encoding="utf-8")
        assert "Alice" in draft
        assert "Acme Parents Association" in draft
        assert "team@acme.example" in draft


def test_drafted_routes_to_processed_and_creates_draft() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        _drop_in_inbox(cfg, _build_eml(subject="Need your input on grant"))
        backend = _StubBackend(
            classifier_response={
                "bucket": "drafted",
                "archive_reason": None,
                "topic_slugs": ["grants"],
                "reason": "needs treasurer input",
            },
            draft_text="Hi Alice, here are my thoughts.",
        )
        report = pipeline.process_inbox(cfg, backend=backend)
        assert report.drafted == 1
        # Email moved out of inbox to processed/
        assert list(cfg.inbox_dir.iterdir()) == []
        moved = [p for p in cfg.processed_dir.iterdir() if p.is_dir() and p.name not in ("archived", "spam")]
        assert len(moved) == 1
        # Classification persisted
        meta = json.loads((moved[0] / "meta.json").read_text(encoding="utf-8"))
        assert meta["classification"]["bucket"] == BUCKET_DRAFTED
        # Draft created
        drafts = list(cfg.queue_dir.glob("*.md"))
        assert len(drafts) == 1
        assert "Hi Alice" in drafts[0].read_text(encoding="utf-8")


def test_flagged_routes_to_processed_and_appends_pending_attention() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        _drop_in_inbox(cfg, _build_eml(subject="Decision needed", body="please decide"))
        backend = _StubBackend(
            classifier_response={
                "bucket": "flagged",
                "archive_reason": None,
                "topic_slugs": [],
                "reason": "needs your decision",
            },
        )
        report = pipeline.process_inbox(cfg, backend=backend)
        assert report.flagged == 1
        text = cfg.pending_attention_path.read_text(encoding="utf-8")
        assert "Decision needed" in text
        assert "needs your decision" in text


def test_ignored_topic_archive_does_not_create_draft() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        memory.save_section(
            cfg, "ignored_topics", "- Library cleanup (keywords: library, cleanup)\n"
        )
        _drop_in_inbox(cfg, _build_eml(subject="Library cleanup this Saturday"))
        report = pipeline.process_inbox(cfg, backend=None)
        assert report.archived == 1
        assert report.drafted == 0
        assert list(cfg.queue_dir.glob("*.md")) == []
        # Folder went to archived/
        archived = list(cfg.archived_dir.iterdir())
        assert len(archived) == 1
        meta = json.loads((archived[0] / "meta.json").read_text(encoding="utf-8"))
        assert meta["classification"]["archive_reason"] == ARCHIVE_IGNORED_TOPIC


def test_spam_routes_to_spam_dir() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        _drop_in_inbox(cfg, _build_eml(sender="mailer-daemon@x.com", subject="Delivery Status Notification"))
        report = pipeline.process_inbox(cfg, backend=None)
        assert report.spam == 1
        assert len(list(cfg.spam_dir.iterdir())) == 1
        assert len(list(cfg.archived_dir.iterdir())) == 0


def test_drafted_without_backend_logs_warning_but_still_routes() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        _drop_in_inbox(cfg, _build_eml(subject="Whatever"))
        # No rule matches and no backend → defaults to flagged-by-default.
        report = pipeline.process_inbox(cfg, backend=None)
        # No draft because no backend was wired; routing still works.
        assert report.processed == 1
        assert list(cfg.queue_dir.glob("*.md")) == []


def test_classification_cache_is_persisted() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        _drop_in_inbox(cfg, _build_eml(subject="Hello", message_id="<persist@x>"))
        backend = _StubBackend(
            classifier_response={"bucket": "flagged", "archive_reason": None, "topic_slugs": [], "reason": "tbd"},
        )
        pipeline.process_inbox(cfg, backend=backend)
        cache_path = cfg.project_root / pipeline.CACHE_FILENAME
        assert cache_path.exists()
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        assert "<persist@x>" in cache


def test_pipeline_cli_accepts_backfill_and_max_per_run_flags() -> None:
    """The CLI should expose --backfill DAYS and --max-per-run N."""
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            pipeline._cli_main(["--help"])
        except SystemExit:
            pass
    out = buf.getvalue()
    assert "--backfill" in out
    assert "--max-per-run" in out
    assert "DAYS" in out


def test_run_pipeline_with_only_process_step() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        _drop_in_inbox(cfg, _build_eml(subject="Whatever"))
        report = pipeline.run_pipeline(cfg, backend=None, fetch=False, process=True)
        assert report.processed == 1
        # Stuck detection writes pipeline_health.json
        health = cfg.project_root / pipeline.HEALTH_FILENAME
        assert health.exists()


# ---------- recipient filter tests ---------------------------------------


def test_passes_recipient_filter_addressed_when_in_to() -> None:
    cfg = Config()
    cfg.user.email = "alice@example.com"
    cfg.pipeline.recipient_filter = "addressed"
    from jarlis.models import Email
    e = Email(message_id="<m>", sender="bob@x", to=["alice@example.com"], cc=[])
    ok, _ = pipeline.passes_recipient_filter(cfg, e)
    assert ok


def test_passes_recipient_filter_addressed_skips_when_not_addressed() -> None:
    cfg = Config()
    cfg.user.email = "alice@example.com"
    cfg.pipeline.recipient_filter = "addressed"
    from jarlis.models import Email
    e = Email(message_id="<m>", sender="bob@x", to=["someone@x"], cc=["another@y"])
    ok, why = pipeline.passes_recipient_filter(cfg, e)
    assert ok is False
    assert "not in To/Cc" in why


def test_passes_recipient_filter_primary_skips_cc_only() -> None:
    cfg = Config()
    cfg.user.email = "alice@example.com"
    cfg.pipeline.recipient_filter = "primary"
    from jarlis.models import Email
    e = Email(message_id="<m>", sender="bob@x", to=["someone@x"], cc=["alice@example.com"])
    ok, _ = pipeline.passes_recipient_filter(cfg, e)
    assert ok is False


def test_passes_recipient_filter_exclusive() -> None:
    cfg = Config()
    cfg.user.email = "alice@example.com"
    cfg.pipeline.recipient_filter = "exclusive"
    from jarlis.models import Email
    e1 = Email(message_id="<m>", sender="bob@x", to=["alice@example.com"], cc=[])
    e2 = Email(message_id="<m>", sender="bob@x", to=["alice@example.com", "bob@x"], cc=[])
    assert pipeline.passes_recipient_filter(cfg, e1)[0] is True
    assert pipeline.passes_recipient_filter(cfg, e2)[0] is False


def test_passes_recipient_filter_all_passes_everything() -> None:
    cfg = Config()
    cfg.user.email = "alice@example.com"
    cfg.pipeline.recipient_filter = "all"
    from jarlis.models import Email
    e = Email(message_id="<m>", sender="bob@x", to=["randoms@list.com"], cc=[])
    assert pipeline.passes_recipient_filter(cfg, e)[0] is True


def test_passes_recipient_filter_aliases_count_as_user() -> None:
    cfg = Config()
    cfg.user.email = "alice@example.com"
    cfg.user.email_aliases = ["alice+ml@example.com", "ali@old.tld"]
    cfg.pipeline.recipient_filter = "addressed"
    from jarlis.models import Email
    e = Email(message_id="<m>", sender="bob@x", to=["ali@old.tld"], cc=[])
    assert pipeline.passes_recipient_filter(cfg, e)[0] is True


def test_passes_recipient_filter_handles_display_name_format() -> None:
    cfg = Config()
    cfg.user.email = "alice@example.com"
    cfg.pipeline.recipient_filter = "addressed"
    from jarlis.models import Email
    e = Email(
        message_id="<m>",
        sender="bob@x",
        to=['"Alice Smith" <alice@example.com>'],
        cc=[],
    )
    assert pipeline.passes_recipient_filter(cfg, e)[0] is True


def test_recipient_filter_archives_with_not_addressed_reason() -> None:
    """End-to-end: an email failing the filter goes to archive/ with reason."""
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.pipeline.recipient_filter = "addressed"
        # Build an email that does NOT have the user in To/Cc
        eml = _build_eml(subject="Group thread")
        # Override To: so the user (you@you.tld) is absent.
        import email.message
        msg = email.message_from_bytes(eml, policy=__import__('email').policy.compat32)
        del msg["To"]
        msg["To"] = "team@list.com"
        eml = bytes(msg)

        _drop_in_inbox(cfg, eml)
        report = pipeline.process_inbox(cfg, backend=None)
        assert report.not_addressed == 1
        assert report.processed == 1
        archived = list(cfg.archived_dir.iterdir())
        assert len(archived) == 1
        meta = json.loads((archived[0] / "meta.json").read_text(encoding="utf-8"))
        assert meta["classification"]["archive_reason"] == "not_addressed"
        assert list(cfg.queue_dir.glob("*.md")) == []  # no draft
