"""Tests for draft_delivery.py: drives a fake SMTP to verify wire format."""

from __future__ import annotations

import smtplib
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from jarlis import draft_delivery, notify
from jarlis.config import Config, init_paths
from jarlis.models import Classification, Email


def _make_cfg(tmp: Path, *, mode: str = "email", lang: str = "en") -> Config:
    cfg = Config()
    cfg.project_root = tmp
    init_paths(cfg)
    cfg.user.firstname = "Alice"
    cfg.user.email = "alice@org.tld"
    cfg.user.languages = [lang]
    cfg.organization.name = "ACME"
    cfg.imap.username = "alice@org.tld"
    cfg.imap.password = "pw"
    cfg.smtp.server = "smtp.example.com"
    cfg.smtp.port = 587
    cfg.smtp.username = "alice@org.tld"
    cfg.smtp.password = "pw"
    cfg.notification.to = "alice@personal.tld"
    cfg.drafts.mode = mode
    return cfg


def _email(**overrides) -> Email:
    defaults = dict(
        message_id="<m-1@x>",
        sender="bob@partner.tld",
        sender_name="Bob",
        subject="Project update",
        body_text="Here is the latest status of project X. We're ahead of schedule.",
        date=datetime(2026, 5, 7, 10, 0),
        to=["alice@org.tld"],
    )
    defaults.update(overrides)
    return Email(**defaults)


def _classification() -> Classification:
    return Classification(
        bucket="drafted",
        topic_slugs=["projects"],
        reason="needs treasurer input",
    )


class _FakeSMTP:
    """Captures sent EmailMessage objects for assertions."""

    instances: list["_FakeSMTP"] = []

    def __init__(self, host: str, port: int, timeout: int = 30) -> None:
        self.host = host
        self.port = port
        self.tls = False
        self.logged_in: tuple[str, str] | None = None
        self.sent: list = []
        _FakeSMTP.instances.append(self)

    def __enter__(self) -> "_FakeSMTP":
        return self

    def __exit__(self, *exc) -> None:
        return None

    def ehlo(self) -> None:
        pass

    def starttls(self) -> None:
        self.tls = True

    def login(self, user: str, pw: str) -> None:
        self.logged_in = (user, pw)

    def send_message(self, msg) -> None:
        self.sent.append(msg)


def _patch(monkeypatch_storage: dict) -> None:
    monkeypatch_storage["orig"] = smtplib.SMTP
    _FakeSMTP.instances = []
    smtplib.SMTP = _FakeSMTP  # type: ignore[assignment]


def _unpatch(monkeypatch_storage: dict) -> None:
    smtplib.SMTP = monkeypatch_storage["orig"]  # type: ignore[assignment]


# ---------- mode dispatch -------------------------------------------------


def test_mode_file_sends_no_email(monkeypatch_storage: dict) -> None:
    _patch(monkeypatch_storage)
    try:
        with tempfile.TemporaryDirectory() as t:
            cfg = _make_cfg(Path(t), mode="file")
            ok = draft_delivery.deliver_draft(cfg, _email(), _classification(), "draft body")
            assert ok is True  # "no notification requested" counts as success
            assert _FakeSMTP.instances == []
    finally:
        _unpatch(monkeypatch_storage)


def test_mode_email_sends_with_reply_to(monkeypatch_storage: dict) -> None:
    _patch(monkeypatch_storage)
    try:
        with tempfile.TemporaryDirectory() as t:
            cfg = _make_cfg(Path(t), mode="email")
            email_obj = _email()
            cls = _classification()
            ok = draft_delivery.deliver_draft(cfg, email_obj, cls, "Hi Bob, thanks for the update.")
            assert ok is True
            assert len(_FakeSMTP.instances) == 1
            sent = _FakeSMTP.instances[0].sent[0]
            assert sent["To"] == "alice@personal.tld"
            assert sent["From"] == "alice@org.tld"
            assert sent["Reply-To"] == "bob@partner.tld"
            assert "ACME" in sent["Subject"]
            assert "Project update" in sent["Subject"]
            payload = sent.get_payload()
            assert isinstance(payload, str)
            assert "Hi Alice" in payload
            assert "Project update" in payload
            assert "Hi Bob, thanks for the update" in payload
            assert "needs treasurer input" in payload
    finally:
        _unpatch(monkeypatch_storage)


def test_mode_email_with_french_locale_uses_french_strings(monkeypatch_storage: dict) -> None:
    _patch(monkeypatch_storage)
    try:
        with tempfile.TemporaryDirectory() as t:
            cfg = _make_cfg(Path(t), mode="email", lang="fr")
            ok = draft_delivery.deliver_draft(cfg, _email(), _classification(), "Bonjour Bob...")
            assert ok is True
            sent = _FakeSMTP.instances[0].sent[0]
            # get_content() decodes QP; get_payload() returns the wire form
            content = sent.get_content()
            assert "Brouillon" in sent["Subject"]
            assert "Bonjour Alice" in content
            assert "Email d'origine" in content
            assert "Brouillon proposé" in content
    finally:
        _unpatch(monkeypatch_storage)


def test_email_mode_skips_when_notification_to_unset(monkeypatch_storage: dict) -> None:
    _patch(monkeypatch_storage)
    try:
        with tempfile.TemporaryDirectory() as t:
            cfg = _make_cfg(Path(t), mode="email")
            cfg.notification.to = ""
            ok = draft_delivery.deliver_draft(cfg, _email(), _classification(), "draft")
            assert ok is False
            assert _FakeSMTP.instances == []
    finally:
        _unpatch(monkeypatch_storage)


def test_unknown_mode_falls_back_to_email(monkeypatch_storage: dict) -> None:
    _patch(monkeypatch_storage)
    try:
        with tempfile.TemporaryDirectory() as t:
            cfg = _make_cfg(Path(t), mode="garbage")
            ok = draft_delivery.deliver_draft(cfg, _email(), _classification(), "draft body")
            assert ok is True
            assert len(_FakeSMTP.instances) == 1
    finally:
        _unpatch(monkeypatch_storage)


def test_local_path_appears_in_body_when_provided(monkeypatch_storage: dict) -> None:
    _patch(monkeypatch_storage)
    try:
        with tempfile.TemporaryDirectory() as t:
            cfg = _make_cfg(Path(t), mode="email")
            local = cfg.queue_dir / "20260507_project_update.md"
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_text("dummy", encoding="utf-8")
            draft_delivery.deliver_draft(
                cfg, _email(), _classification(), "draft", local_path=local,
            )
            sent = _FakeSMTP.instances[0].sent[0]
            payload = sent.get_payload()
            assert "20260507_project_update.md" in payload
    finally:
        _unpatch(monkeypatch_storage)


def test_long_original_body_is_truncated(monkeypatch_storage: dict) -> None:
    _patch(monkeypatch_storage)
    try:
        with tempfile.TemporaryDirectory() as t:
            cfg = _make_cfg(Path(t), mode="email")
            email_obj = _email()
            email_obj.body_text = "A" * 10_000
            draft_delivery.deliver_draft(cfg, email_obj, _classification(), "draft")
            sent = _FakeSMTP.instances[0].sent[0]
            content = sent.get_content()
            assert "truncated by JARLIS" in content
            # Body in the email is capped near the configured limit
            assert content.count("A") <= draft_delivery.ORIGINAL_QUOTE_LIMIT + 100
    finally:
        _unpatch(monkeypatch_storage)


def test_notification_includes_to_cc_and_attachments(monkeypatch_storage: dict) -> None:
    _patch(monkeypatch_storage)
    try:
        with tempfile.TemporaryDirectory() as t:
            cfg = _make_cfg(Path(t), mode="email")
            email_obj = _email(
                to=["alice@org.tld", "carol@org.tld"],
                cc=["dave@partner.tld"],
                attachments=["report.pdf", "budget.xlsx"],
            )
            ok = draft_delivery.deliver_draft(cfg, email_obj, _classification(), "draft body")
            assert ok is True
            content = _FakeSMTP.instances[0].sent[0].get_content()
            assert "alice@org.tld" in content
            assert "carol@org.tld" in content
            assert "Cc:      dave@partner.tld" in content
            assert "report.pdf" in content
            assert "budget.xlsx" in content
            assert "Attachments" in content
    finally:
        _unpatch(monkeypatch_storage)


def test_notification_with_attachment_paths_shows_full_path_and_preview(monkeypatch_storage: dict) -> None:
    _patch(monkeypatch_storage)
    try:
        with tempfile.TemporaryDirectory() as t:
            cfg = _make_cfg(Path(t), mode="email")
            # Materialize one extractable attachment on disk
            att_dir = cfg.attachments_dir / "20260507_msg-1@x"
            att_dir.mkdir(parents=True, exist_ok=True)
            att = att_dir / "memo.txt"
            att.write_text("budget proposal: 2026 line items follow...", encoding="utf-8")
            from jarlis import attachments as att_extract
            att_extract.extract_and_save(att)

            email_obj = _email(attachments=["memo.txt"])
            ok = draft_delivery.deliver_draft(
                cfg, email_obj, _classification(), "draft body",
                attachment_paths=[att],
            )
            assert ok is True
            content = _FakeSMTP.instances[0].sent[0].get_content()
            # Full absolute path + extracted-text path + content preview
            assert str(att) in content
            assert "memo.txt.extracted.txt" in content
            assert "budget proposal" in content
    finally:
        _unpatch(monkeypatch_storage)
