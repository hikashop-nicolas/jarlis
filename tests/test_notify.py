"""Tests for notify.py: drives a fake smtplib.SMTP for assertions."""

from __future__ import annotations

import tempfile
from pathlib import Path

from jarlis import notify
from jarlis.config import Config, init_paths


def _make_cfg(tmp: Path) -> Config:
    cfg = Config()
    cfg.project_root = tmp
    init_paths(cfg)
    cfg.smtp.server = "smtp.example.com"
    cfg.smtp.port = 587
    cfg.smtp.username = "alice@example.com"
    cfg.smtp.password = "pw"
    cfg.imap.username = "alice@example.com"
    cfg.imap.password = "pw"
    cfg.notification.to = "notify@example.com"
    cfg.user.firstname = "Alice"
    cfg.user.languages = ["en"]
    cfg.organization.name = "ACME"
    return cfg


class _FakeSMTP:
    """Minimal stand-in for ``smtplib.SMTP`` covering the call surface notify uses."""

    instances: list["_FakeSMTP"] = []

    def __init__(self, host: str, port: int, timeout: int = 30) -> None:
        self.host = host
        self.port = port
        self.ehlo_calls = 0
        self.tls = False
        self.logged_in: tuple[str, str] | None = None
        self.sent: list = []
        _FakeSMTP.instances.append(self)

    def __enter__(self) -> "_FakeSMTP":
        return self

    def __exit__(self, *exc) -> None:
        return None

    def ehlo(self) -> None:
        self.ehlo_calls += 1

    def starttls(self) -> None:
        self.tls = True

    def login(self, user: str, pw: str) -> None:
        self.logged_in = (user, pw)

    def send_message(self, msg) -> None:
        self.sent.append(msg)


def _patch_smtp(monkeypatch_storage: dict) -> None:
    monkeypatch_storage["orig_smtp"] = notify.smtplib.SMTP
    _FakeSMTP.instances = []
    notify.smtplib.SMTP = _FakeSMTP  # type: ignore[assignment]


def _restore_smtp(monkeypatch_storage: dict) -> None:
    notify.smtplib.SMTP = monkeypatch_storage["orig_smtp"]  # type: ignore[assignment]


def test_send_email_uses_starttls_and_logs_in(monkeypatch_storage: dict) -> None:
    _patch_smtp(monkeypatch_storage)
    try:
        with tempfile.TemporaryDirectory() as t:
            cfg = _make_cfg(Path(t))
            ok = notify.send_email(cfg, "Hello", "Body line")
            assert ok is True
            assert len(_FakeSMTP.instances) == 1
            inst = _FakeSMTP.instances[0]
            assert inst.host == "smtp.example.com"
            assert inst.tls is True
            assert inst.logged_in == ("alice@example.com", "pw")
            assert len(inst.sent) == 1
            sent_msg = inst.sent[0]
            assert sent_msg["Subject"] == "Hello"
            assert sent_msg["To"] == "notify@example.com"
            assert sent_msg["From"] == "alice@example.com"
    finally:
        _restore_smtp(monkeypatch_storage)


def test_send_email_skips_when_recipient_missing(monkeypatch_storage: dict) -> None:
    _patch_smtp(monkeypatch_storage)
    try:
        with tempfile.TemporaryDirectory() as t:
            cfg = _make_cfg(Path(t))
            cfg.notification.to = ""
            ok = notify.send_email(cfg, "x", "y")
            assert ok is False
            assert _FakeSMTP.instances == []
    finally:
        _restore_smtp(monkeypatch_storage)


def test_send_email_skips_when_smtp_server_missing(monkeypatch_storage: dict) -> None:
    _patch_smtp(monkeypatch_storage)
    try:
        with tempfile.TemporaryDirectory() as t:
            cfg = _make_cfg(Path(t))
            cfg.smtp.server = ""
            ok = notify.send_email(cfg, "x", "y")
            assert ok is False
            assert _FakeSMTP.instances == []
    finally:
        _restore_smtp(monkeypatch_storage)


def test_send_email_returns_false_on_smtp_error(monkeypatch_storage: dict) -> None:
    class _BadSMTP(_FakeSMTP):
        def send_message(self, msg) -> None:
            import smtplib as _smtplib
            raise _smtplib.SMTPException("simulated failure")

    monkeypatch_storage["orig_smtp"] = notify.smtplib.SMTP
    notify.smtplib.SMTP = _BadSMTP  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as t:
            cfg = _make_cfg(Path(t))
            ok = notify.send_email(cfg, "Hello", "Body")
            assert ok is False
    finally:
        notify.smtplib.SMTP = monkeypatch_storage["orig_smtp"]  # type: ignore[assignment]


def test_send_test_ping_uses_i18n(monkeypatch_storage: dict) -> None:
    _patch_smtp(monkeypatch_storage)
    try:
        with tempfile.TemporaryDirectory() as t:
            cfg = _make_cfg(Path(t))
            cfg.user.languages = ["fr"]
            assert notify.send_test_ping(cfg) is True
            sent = _FakeSMTP.instances[0].sent[0]
            assert "Test JARLIS" in sent["Subject"]
            assert "ACME" in sent["Subject"]
            payload = sent.get_payload()
            assert isinstance(payload, str)
            assert "Bonjour Alice" in payload
    finally:
        _restore_smtp(monkeypatch_storage)


def test_send_stuck_alert_includes_count_and_history(monkeypatch_storage: dict) -> None:
    _patch_smtp(monkeypatch_storage)
    try:
        with tempfile.TemporaryDirectory() as t:
            cfg = _make_cfg(Path(t))
            assert notify.send_stuck_alert(
                cfg, count=12, threshold=3, history=[5, 8, 12], log_path="/var/log/jarlis.log"
            ) is True
            sent = _FakeSMTP.instances[0].sent[0]
            assert "12" in sent["Subject"]
            payload = sent.get_payload()
            assert "5, 8, 12" in payload
            assert "/var/log/jarlis.log" in payload
    finally:
        _restore_smtp(monkeypatch_storage)


def test_sanitize_header_flattens_folded_newlines() -> None:
    from jarlis import notify
    # A folded IMAP subject that previously crashed EmailMessage.
    s = "Fwd: Formations professionnelles 2026 : confirmation du traitement de\n paie (Excel + FLE)"
    out = notify._sanitize_header(s)
    assert "\n" not in out and "\r" not in out
    assert out == "Fwd: Formations professionnelles 2026 : confirmation du traitement de paie (Excel + FLE)"
    # CRLF and surrounding whitespace collapse to a single space.
    assert notify._sanitize_header("a\r\n\tb") == "a b"
    assert notify._sanitize_header("  spaced  ") == "spaced"
    assert notify._sanitize_header("") == ""


def test_sanitized_subject_is_accepted_by_emailmessage() -> None:
    from email.message import EmailMessage
    from jarlis import notify
    msg = EmailMessage()
    # Would raise ValueError without sanitization.
    msg["Subject"] = notify._sanitize_header("line one\nline two")
    assert msg["Subject"] == "line one line two"
