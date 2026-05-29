"""Tests for imap_fetch.py: pure parsing + on-disk roundtrip.

These tests do NOT connect to a real IMAP server. End-to-end IMAP
testing is left for the user to run against their own account.
"""

from __future__ import annotations

import email.policy
import json
import tempfile
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from jarlis import imap_fetch
from jarlis.config import Config, init_paths


def _make_cfg(tmp: Path) -> Config:
    cfg = Config()
    cfg.project_root = tmp
    init_paths(cfg)
    return cfg


def _build_message(
    *,
    sender: str = "Alice <alice@example.com>",
    to: str = "you@you.tld",
    subject: str = "Hello",
    body: str = "hi there",
    html: str | None = None,
    message_id: str = "<msg-1@x>",
    in_reply_to: str = "",
    references: str = "",
    date: str = "Thu, 7 May 2026 10:00:00 +0000",
    attachments: list[tuple[str, bytes, str]] | None = None,
) -> bytes:
    """Build a serialized RFC822 message for parsing tests."""
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg["Date"] = date
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    if attachments:
        for filename, data, ctype in attachments:
            maintype, _, subtype = ctype.partition("/")
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return bytes(msg)


# ---------- helpers -------------------------------------------------------


def test_decode_header_value_handles_encoded_words() -> None:
    raw = "=?UTF-8?B?44GT44KT44Gr44Gh44Gv?="  # "こんにちは"
    out = imap_fetch.decode_header_value(raw)
    assert "こんにちは" in out


def test_decode_header_value_empty() -> None:
    assert imap_fetch.decode_header_value("") == ""
    assert imap_fetch.decode_header_value(None) == ""


def test_extract_email_address_strips_display_name() -> None:
    assert imap_fetch.extract_email_address("Alice Smith <alice@x.com>") == "alice@x.com"
    assert imap_fetch.extract_email_address("ALICE@X.COM") == "alice@x.com"
    assert imap_fetch.extract_email_address("") == ""


def test_extract_display_name_keeps_unicode() -> None:
    # Build a properly RFC-2047-encoded display name with a non-ASCII string;
    # exercises the unicode path without baking real names into the test.
    from email.header import Header
    name = "タナカ"
    encoded = Header(name, "utf-8").encode()
    raw = f"{encoded} <user@x.com>"
    assert imap_fetch.extract_display_name(raw) == name


def test_parse_address_list_splits_on_commas() -> None:
    addrs = imap_fetch.parse_address_list("Alice <a@x>, Bob <b@y>, c@z")
    assert len(addrs) == 3


def test_make_email_folder_name_uses_date_and_short_id() -> None:
    raw = _build_message(message_id="<abc-DEF-123@x>")
    msg = __import__("email").message_from_bytes(raw, policy=email.policy.compat32)
    name = imap_fetch.make_email_folder_name(msg)
    assert name.startswith("20260507_100000_")
    assert "abcDEF123" in name


# ---------- write_email + load_email_from_folder roundtrip ----------------


def test_write_and_load_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.inbox_dir.mkdir(parents=True, exist_ok=True)
        cfg.attachments_dir.mkdir(parents=True, exist_ok=True)

        raw = _build_message(
            sender="Alice <alice@example.com>",
            subject="Project update",
            body="Here is the status of project X.",
            html="<p>Here is the status of project X.</p>",
            message_id="<id-1@x>",
        )
        result = imap_fetch.write_email(cfg, raw)
        assert result is not None
        folder, meta = result
        assert meta["sender"] == "alice@example.com"
        assert meta["sender_name"] == "Alice"
        assert meta["subject"] == "Project update"
        assert (folder / "raw.eml").exists()
        assert (folder / "body.txt").exists()
        assert (folder / "body.html").exists()

        loaded = imap_fetch.load_email_from_folder(folder)
        assert loaded is not None
        assert loaded.sender == "alice@example.com"
        assert loaded.subject == "Project update"
        assert "project X" in loaded.body_text


def test_attachments_are_saved_under_attachments_dir() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.inbox_dir.mkdir(parents=True, exist_ok=True)
        cfg.attachments_dir.mkdir(parents=True, exist_ok=True)
        raw = _build_message(
            attachments=[
                ("report.pdf", b"%PDF-1.4 fake", "application/pdf"),
                ("資料.docx", b"<docx-bytes>", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            ],
        )
        folder, meta = imap_fetch.write_email(cfg, raw)
        assert meta["attachment_count"] == 2
        att_dir = cfg.attachments_dir / folder.name
        files = sorted(p.name for p in att_dir.iterdir())
        assert any("report" in f for f in files)
        # The Japanese filename is hashed to ASCII
        assert all(all(c.isascii() for c in f) for f in files)


def test_write_email_skips_existing_folder() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.inbox_dir.mkdir(parents=True, exist_ok=True)
        cfg.attachments_dir.mkdir(parents=True, exist_ok=True)
        raw = _build_message(message_id="<dup-1@x>")
        first = imap_fetch.write_email(cfg, raw)
        assert first is not None
        second = imap_fetch.write_email(cfg, raw)
        assert second is None  # no-op for duplicate


def test_in_reply_to_and_references_are_preserved() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.inbox_dir.mkdir(parents=True, exist_ok=True)
        cfg.attachments_dir.mkdir(parents=True, exist_ok=True)
        raw = _build_message(
            message_id="<reply-1@x>",
            in_reply_to="<root@x>",
            references="<root@x> <middle@x>",
        )
        folder, meta = imap_fetch.write_email(cfg, raw)
        assert meta["in_reply_to"] == "<root@x>"
        assert meta["references"] == ["<root@x>", "<middle@x>"]
        loaded = imap_fetch.load_email_from_folder(folder)
        assert loaded.in_reply_to == "<root@x>"
        assert "<middle@x>" in loaded.references


# ---------- state file helpers --------------------------------------------


def test_seen_ids_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        assert imap_fetch.load_seen_ids(cfg) == set()
        imap_fetch.save_seen_ids(cfg, {"a", "b", "c"})
        assert imap_fetch.load_seen_ids(cfg) == {"a", "b", "c"}


def test_last_fetch_timestamp_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        assert imap_fetch.get_last_fetch_timestamp(cfg) is None
        imap_fetch.save_last_fetch_timestamp(cfg, 1234567890)
        assert imap_fetch.get_last_fetch_timestamp(cfg) == 1234567890


def test_quote_mailbox_quotes_when_needed() -> None:
    assert imap_fetch._quote_mailbox("INBOX") == "INBOX"
    assert imap_fetch._quote_mailbox("Sent") == "Sent"
    assert imap_fetch._quote_mailbox("INBOX.Drafts") == "INBOX.Drafts"
    # Spaces require quoting
    assert imap_fetch._quote_mailbox("Sent Items") == '"Sent Items"'
    # Brackets require quoting (Gmail folders)
    assert imap_fetch._quote_mailbox("[Gmail]/Sent Mail") == '"[Gmail]/Sent Mail"'
    # Embedded quotes get escaped
    assert imap_fetch._quote_mailbox('weird"name') == '"weird\\"name"'


def test_iter_inbox_returns_sorted_folders() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.inbox_dir.mkdir(parents=True, exist_ok=True)
        for name in ("c", "a", "b"):
            (cfg.inbox_dir / name).mkdir()
        names = [p.name for p in imap_fetch.iter_inbox(cfg)]
        assert names == ["a", "b", "c"]
