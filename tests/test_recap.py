"""Tests for recap.py."""

from __future__ import annotations

import json
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from jarlis import memory, recap
from jarlis.config import Config, init_paths
from jarlis.models import (
    ARCHIVE_IGNORED_TOPIC,
    ARCHIVE_LOW_PRIORITY,
    ARCHIVE_SPAM,
    BUCKET_ARCHIVE,
    BUCKET_DRAFTED,
    BUCKET_FLAGGED,
    Classification,
)


def _make_cfg(tmp: Path) -> Config:
    cfg = Config()
    cfg.project_root = tmp
    init_paths(cfg)
    cfg.organization.name = "ACME"
    cfg.user.firstname = "Alice"
    cfg.user.languages = ["en"]
    return cfg


def _drop_processed(
    parent: Path,
    *,
    name: str,
    sender: str,
    subject: str,
    bucket: str,
    archive_reason: str | None = None,
    topic_slugs: list[str] | None = None,
    days_ago: int = 0,
    reason: str = "test reason",
    attachments: list[str] | None = None,
) -> Path:
    folder = parent / name
    folder.mkdir(parents=True, exist_ok=True)
    cls = Classification(
        bucket=bucket,
        archive_reason=archive_reason,
        topic_slugs=topic_slugs or [],
        reason=reason,
    )
    meta = {
        "sender": sender,
        "sender_name": sender.split("@")[0],
        "subject": subject,
        "date": (date.today() - timedelta(days=days_ago)).isoformat() + "T10:00:00",
        "classification": cls.to_dict(),
        "attachments": attachments or [],
    }
    (folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return folder


# ---------- gating -------------------------------------------------------


def test_is_due_today_daily_first_run() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.recap.frequency = "daily"
        assert recap.is_due_today(cfg) is True


def test_is_due_today_daily_after_running() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.recap.frequency = "daily"
        recap._save_last_run(cfg, date.today())
        assert recap.is_due_today(cfg) is False


def test_is_due_today_every_n_days() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.recap.frequency = "every_n_days"
        cfg.recap.n_days = 3
        recap._save_last_run(cfg, date.today() - timedelta(days=2))
        assert recap.is_due_today(cfg) is False
        recap._save_last_run(cfg, date.today() - timedelta(days=3))
        assert recap.is_due_today(cfg) is True


def test_is_due_today_weekly_matches_weekday() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.recap.frequency = "weekly"
        today = date.today()
        cfg.recap.weekday = today.strftime("%a").lower()
        assert recap.is_due_today(cfg, today) is True
        # Different weekday → not due
        non_match = "mon" if cfg.recap.weekday != "mon" else "tue"
        cfg.recap.weekday = non_match
        # Skip if today happens to match the alternative: pick a weekday that's not today
        wrong = "mon"
        for cand in ("mon", "tue", "wed", "thu", "fri", "sat", "sun"):
            if not today.strftime("%a").lower().startswith(cand):
                wrong = cand
                break
        cfg.recap.weekday = wrong
        assert recap.is_due_today(cfg, today) is False


def test_is_due_today_disabled() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.recap.enabled = False
        assert recap.is_due_today(cfg) is False


# ---------- collection ---------------------------------------------------


def test_collect_recap_content_routes_each_bucket() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.processed_dir.mkdir(parents=True, exist_ok=True)
        cfg.archived_dir.mkdir(parents=True, exist_ok=True)
        cfg.spam_dir.mkdir(parents=True, exist_ok=True)
        cfg.queue_dir.mkdir(parents=True, exist_ok=True)

        _drop_processed(cfg.processed_dir, name="d1", sender="alice@x", subject="Need input", bucket=BUCKET_DRAFTED)
        _drop_processed(cfg.processed_dir, name="f1", sender="bob@y", subject="Decide on X", bucket=BUCKET_FLAGGED)
        _drop_processed(cfg.archived_dir, name="i1", sender="lib@z", subject="Cleanup",
                        bucket=BUCKET_ARCHIVE, archive_reason=ARCHIVE_IGNORED_TOPIC,
                        topic_slugs=["library_cleanup"])
        _drop_processed(cfg.archived_dir, name="i2", sender="lib@z", subject="Cleanup 2",
                        bucket=BUCKET_ARCHIVE, archive_reason=ARCHIVE_IGNORED_TOPIC,
                        topic_slugs=["library_cleanup"])
        _drop_processed(cfg.archived_dir, name="lp1", sender="news@x", subject="Newsletter",
                        bucket=BUCKET_ARCHIVE, archive_reason=ARCHIVE_LOW_PRIORITY)
        _drop_processed(cfg.spam_dir, name="s1", sender="spam@x", subject="Junk",
                        bucket=BUCKET_ARCHIVE, archive_reason=ARCHIVE_SPAM)

        content = recap.collect_recap_content(cfg)
        assert len(content.drafted) == 1
        assert len(content.flagged) == 1
        assert "library_cleanup" in content.ignored_by_topic
        assert len(content.ignored_by_topic["library_cleanup"]) == 2
        assert len(content.low_priority) == 1
        # Spam stays out of the recap entirely.
        assert content.has_content is True


def test_collect_old_drafts_finds_pending_files() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.queue_dir.mkdir(parents=True, exist_ok=True)
        cfg.cleanup.draft_pending_days = 7
        old = cfg.queue_dir / "old_draft.md"
        old.write_text("# Draft\nTo: alice@x\nSubject: Hello\n", encoding="utf-8")
        # Make it "ancient"
        ancient = (datetime.now() - timedelta(days=30)).timestamp()
        import os
        os.utime(old, (ancient, ancient))
        new = cfg.queue_dir / "new_draft.md"
        new.write_text("# Draft\nTo: bob@x\nSubject: World\n", encoding="utf-8")

        content = recap.collect_recap_content(cfg)
        assert len(content.old_drafts) == 1
        assert content.old_drafts[0][0].name == "old_draft.md"


# ---------- rendering ----------------------------------------------------


def test_render_no_activity_message() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        content = recap.RecapContent(
            period_start=date.today() - timedelta(days=1),
            period_end=date.today(),
        )
        subject, body = recap.render_recap(cfg, content)
        assert "ACME" in subject
        assert "JARLIS" in subject
        assert "No activity" in body
        assert "Hi Alice" in body


def test_render_includes_drafted_flagged_ignored_in_file_mode() -> None:
    """In file mode the drafted section is shown (and points the user at
    the queue dir). In email mode the section is suppressed because the
    user already has each draft as a separate notification email."""
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.drafts.mode = "file"  # otherwise the drafted section is hidden
        cfg.processed_dir.mkdir(parents=True, exist_ok=True)
        cfg.archived_dir.mkdir(parents=True, exist_ok=True)
        _drop_processed(cfg.processed_dir, name="d1", sender="alice@x", subject="Need input", bucket=BUCKET_DRAFTED)
        _drop_processed(cfg.archived_dir, name="i1", sender="lib@z", subject="Cleanup",
                        bucket=BUCKET_ARCHIVE, archive_reason=ARCHIVE_IGNORED_TOPIC,
                        topic_slugs=["library"])

        content = recap.collect_recap_content(cfg)
        subject, body = recap.render_recap(cfg, content)
        assert "Drafts pending" in body
        assert "Need input" in body
        assert "Silenced topics" in body
        assert "library" in body
        # File mode points the user at the queue dir.
        assert str(cfg.queue_dir) in body


def test_normalize_thread_subject_strips_recursive_prefixes() -> None:
    norm = recap._normalize_thread_subject
    assert norm("Hello") == "hello"
    assert norm("Re: Hello") == "hello"
    assert norm("re: re: Hello") == "hello"
    assert norm("Fwd: Re: Fwd: Hello") == "hello"
    assert norm("RE[2]: Hello") == "hello"
    assert norm("TR : Réunion mardi") == "réunion mardi"  # French Tr: prefix
    assert norm("") == ""


def test_group_by_thread_collapses_re_fwd_variants() -> None:
    from jarlis.recap import RecapItem
    items = [
        RecapItem(sender="a@x", sender_name="A", subject="Project X update",
                  date_iso="2026-05-09T10:00", folder=Path("/tmp/a"),
                  classification=Classification(bucket="archive")),
        RecapItem(sender="b@x", sender_name="B", subject="Re: Project X update",
                  date_iso="2026-05-09T11:00", folder=Path("/tmp/b"),
                  classification=Classification(bucket="archive")),
        RecapItem(sender="c@x", sender_name="C", subject="Fwd: Re: Project X update",
                  date_iso="2026-05-09T12:00", folder=Path("/tmp/c"),
                  classification=Classification(bucket="archive")),
        RecapItem(sender="d@x", sender_name="D", subject="Different topic",
                  date_iso="2026-05-09T13:00", folder=Path("/tmp/d"),
                  classification=Classification(bucket="archive")),
    ]
    groups = recap._group_by_thread(items)
    assert len(groups) == 2
    assert len(groups["project x update"]) == 3
    assert len(groups["different topic"]) == 1


class _FakeBackend:
    """Stub backend that returns a canned narration for thread collapse tests."""
    name = "fake"
    def __init__(self, text: str = "Three emails about a topic the user can ignore.") -> None:
        self.text = text
        self.calls: list[str] = []
    def call_text(self, prompt: str) -> str:
        self.calls.append(prompt)
        return self.text
    def call_json(self, prompt: str) -> dict:
        return {}


def test_render_collapses_thread_when_three_or_more_in_low_priority() -> None:
    """3+ emails in the same thread → one AI-narrated paragraph instead of 3 blocks."""
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.processed_dir.mkdir(parents=True, exist_ok=True)
        cfg.archived_dir.mkdir(parents=True, exist_ok=True)
        # Three emails on the same thread, all archived as low_priority.
        for i, subj in enumerate(("Status update", "Re: Status update", "Fwd: Re: Status update")):
            _drop_processed(
                cfg.archived_dir, name=f"x{i}", sender=f"u{i}@x", subject=subj,
                bucket="archive", archive_reason="low_priority",
                reason=f"reason {i}",
            )
        # Plus one unrelated low_priority email (should stay as its own block).
        _drop_processed(
            cfg.archived_dir, name="solo", sender="z@x", subject="Solo update",
            bucket="archive", archive_reason="low_priority", reason="solo reason",
        )

        backend = _FakeBackend(text="Three users discuss the status update; FYI only.")
        content = recap.collect_recap_content(cfg)
        _, body = recap.render_recap(cfg, content, backend=backend)

        # The narration replaced the three rich blocks for the collapsed thread.
        assert "Three users discuss the status update" in body
        assert backend.calls, "backend should have been invoked once"
        # The solo email is rendered as a normal block.
        assert "Solo update" in body


def test_render_does_not_collapse_under_threshold() -> None:
    """Two emails in the same thread → still rendered as separate blocks."""
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.archived_dir.mkdir(parents=True, exist_ok=True)
        for i, subj in enumerate(("Quick note", "Re: Quick note")):
            _drop_processed(
                cfg.archived_dir, name=f"q{i}", sender=f"u{i}@x", subject=subj,
                bucket="archive", archive_reason="low_priority", reason="r",
            )
        backend = _FakeBackend()
        content = recap.collect_recap_content(cfg)
        _, body = recap.render_recap(cfg, content, backend=backend)
        # No narration call should have been made.
        assert not backend.calls
        # Both emails appear as separate blocks.
        assert "Quick note" in body
        assert "Re: Quick note" in body


def test_render_thread_narration_disabled_when_backend_is_none() -> None:
    """Without a backend, no narration occurs even with 3+ thread emails."""
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.archived_dir.mkdir(parents=True, exist_ok=True)
        for i, subj in enumerate(("Topic A", "Re: Topic A", "Fwd: Topic A")):
            _drop_processed(
                cfg.archived_dir, name=f"a{i}", sender=f"u{i}@x", subject=subj,
                bucket="archive", archive_reason="low_priority", reason=f"r{i}",
            )
        content = recap.collect_recap_content(cfg)
        _, body = recap.render_recap(cfg, content, backend=None)
        # All three rendered as separate rich blocks (no collapse).
        assert body.count("--- 1 ---") == 1
        assert body.count("--- 2 ---") == 1
        assert body.count("--- 3 ---") == 1


def test_render_skips_drafted_section_in_email_mode() -> None:
    """In email mode (default), drafts already arrived as separate emails;
    listing them in the recap is noise."""
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.drafts.mode = "email"
        cfg.processed_dir.mkdir(parents=True, exist_ok=True)
        _drop_processed(cfg.processed_dir, name="d1", sender="alice@x",
                        subject="Need input", bucket=BUCKET_DRAFTED)
        content = recap.collect_recap_content(cfg)
        _, body = recap.render_recap(cfg, content)
        assert "Drafts pending" not in body
        assert "Need input" not in body


def test_recap_skips_self_sent_emails() -> None:
    """Emails where sender == user.email (or alias) are gmail echoes of
    the user's own outgoing mail. They must not appear in the recap.
    """
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.user.email = "alice@example.com"
        cfg.user.email_aliases = ["alice.work@example.com"]
        cfg.processed_dir.mkdir(parents=True, exist_ok=True)
        # One self-sent (from the user) + one real flagged email
        _drop_processed(cfg.processed_dir, name="self1", sender="alice@example.com",
                        subject="my own outgoing mail", bucket="flagged")
        _drop_processed(cfg.processed_dir, name="self2", sender="Alice <alice.work@example.com>",
                        subject="another from alias", bucket="flagged")
        _drop_processed(cfg.processed_dir, name="real", sender="bob@x.com",
                        subject="real reply", bucket="flagged")
        content = recap.collect_recap_content(cfg)
        assert len(content.flagged) == 1
        assert content.flagged[0].subject == "real reply"


def test_recap_skips_threads_already_drafted() -> None:
    """If a thread had a draft (notification already mailed), other emails
    of the same normalized subject should NOT clutter the flagged section.
    """
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.processed_dir.mkdir(parents=True, exist_ok=True)
        # Drafted email on a thread
        _drop_processed(cfg.processed_dir, name="d1", sender="alice@x",
                        subject="Project X update", bucket="drafted")
        # Same thread but classified flagged → should be suppressed
        _drop_processed(cfg.processed_dir, name="f1", sender="bob@x",
                        subject="Re: Project X update", bucket="flagged")
        # Different thread → should still appear
        _drop_processed(cfg.processed_dir, name="f2", sender="carol@x",
                        subject="Different topic", bucket="flagged")
        content = recap.collect_recap_content(cfg)
        # No drafted entries leaked, and the flagged section only has the
        # unrelated subject.
        flagged_subjects = [it.subject for it in content.flagged]
        assert "Different topic" in flagged_subjects
        assert not any("Project X" in s for s in flagged_subjects)


def test_recap_flagged_section_merges_duplicate_threads() -> None:
    """When the flagged section has two emails sharing a normalized
    subject, they should render as a single block with concatenated motifs."""
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.processed_dir.mkdir(parents=True, exist_ok=True)
        _drop_processed(cfg.processed_dir, name="r1", sender="ish@school",
                        subject="Report on subject A", bucket="flagged",
                        reason="First note about the report.")
        _drop_processed(cfg.processed_dir, name="r2", sender="ish@school",
                        subject="Report on subject A", bucket="flagged",
                        reason="Second note (later send-time variation).")
        content = recap.collect_recap_content(cfg)
        _, body = recap.render_recap(cfg, content)
        # The duplicate isn't rendered twice as separate --- 1 --- / --- 2 --- blocks.
        assert body.count("--- 1 ---") == 1
        assert body.count("--- 2 ---") == 0
        # Both motifs appear once each, in the merged block.
        assert "First note" in body
        assert "Second note" in body
        # Count-of-emails-in-thread marker visible.
        assert "2 emails" in body


def test_classifier_pre_translates_body_when_lang_differs() -> None:
    """When the email body is in a language different from the user's primary
    language, the classifier should pre-translate it via the AI backend before
    asking for a classification. This is what makes ``reason`` reliably come
    back in the user's language instead of mirroring the input language."""
    from jarlis import classify
    from jarlis.config import Config
    from jarlis.models import Email

    cfg = Config()
    cfg.user.languages = ["fr"]

    calls: list[str] = []

    class _SpyBackend:
        name = "spy"
        def call_text(self, prompt: str) -> str:
            calls.append(prompt)
            # First call is the translation, second is the classification.
            if "Translate the following text" in prompt:
                return "Bonjour, je suis le corps traduit en français."
            # classification call → return JSON
            return '{"bucket": "flagged", "archive_reason": null, "topic_slugs": [], "reason": "Test French reason."}'
        def call_json(self, prompt: str) -> dict:
            calls.append(prompt)
            return {"bucket": "flagged", "archive_reason": None, "topic_slugs": [], "reason": "Test French reason."}

    e = Email(
        message_id="<m@x>", sender="alice@example.com", sender_name="Alice",
        subject="お疲れ様です",
        body_text="お疲れ様です。会議の件、よろしくお願いいたします。" * 6,
        to=["bob@example.com"], cc=[],
    )
    cls = classify._classify_via_llm(cfg, e, _SpyBackend())
    # Translation call happened first, then classification call.
    assert any("Translate the following text" in c for c in calls)
    # Classification prompt mentions the translation upstream.
    cls_prompt = next(c for c in calls if "Translate the following text" not in c)
    assert "machine-translated" in cls_prompt
    # And the reason comes back in the user's language (per spy stub).
    assert cls.reason == "Test French reason."


def test_recap_block_includes_attachment_paths() -> None:
    """Attachment files referenced in meta.json should appear in the block
    so the user can click straight to them in their file browser."""
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.processed_dir.mkdir(parents=True, exist_ok=True)
        cfg.attachments_dir.mkdir(parents=True, exist_ok=True)
        # Drop an attachment file on disk so resolution finds it.
        att_dir = cfg.attachments_dir / "f1"
        att_dir.mkdir(parents=True, exist_ok=True)
        att_file = att_dir / "report.pdf"
        att_file.write_text("dummy pdf bytes")
        _drop_processed(cfg.processed_dir, name="f1", sender="ish@school",
                        subject="With attachment", bucket="flagged",
                        attachments=["report.pdf"])
        content = recap.collect_recap_content(cfg)
        _, body = recap.render_recap(cfg, content)
        assert str(att_file) in body
        assert "Attachments" in body or "Pièces jointes" in body


def test_render_flagged_block_includes_subject_sender_reason_and_folder_path() -> None:
    """Flagged emails render as rich blocks."""
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.processed_dir.mkdir(parents=True, exist_ok=True)
        folder = _drop_processed(
            cfg.processed_dir, name="f1", sender="bob@y",
            subject="Decision needed", bucket="flagged",
            reason="Bob wants your call on the schedule.",
        )
        content = recap.collect_recap_content(cfg)
        _, body = recap.render_recap(cfg, content)
        # The subject, the sender email, the reason, and the on-disk path
        # are all present so the user can navigate to body.txt.
        assert "Decision needed" in body
        assert "bob@y" in body
        assert "Bob wants your call" in body
        assert str(folder) in body


def test_render_french_when_user_language_is_fr() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.user.languages = ["fr"]
        content = recap.RecapContent(
            period_start=date.today() - timedelta(days=1),
            period_end=date.today(),
        )
        subject, body = recap.render_recap(cfg, content)
        assert "Récap" in subject
        assert "Aucune activité" in body


# ---------- run_recap end-to-end -----------------------------------------


def test_run_recap_self_gates_when_not_due() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.recap.frequency = "daily"
        recap._save_last_run(cfg, date.today())
        captured = []
        result = recap.run_recap(cfg, sender=lambda s, b: captured.append((s, b)))
        assert result is None
        assert captured == []


def test_run_recap_force_runs_anyway() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        recap._save_last_run(cfg, date.today())
        captured = []
        result = recap.run_recap(cfg, force=True, sender=lambda s, b: captured.append((s, b)))
        assert result is not None
        assert len(captured) == 1


def test_run_recap_updates_last_run_on_success() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.recap.frequency = "daily"
        result = recap.run_recap(cfg, sender=None)
        assert result is not None
        assert recap._last_run(cfg) == date.today()
