"""IMAP fetcher.

Connects to the configured IMAP server, finds messages newer than the
last successful fetch, parses them, and writes one folder per message
under ``cfg.inbox_dir``. The pipeline reads from there.

State files live next to ``config.toml`` (cfg.project_root):
    last_fetch.txt       : epoch seconds of last successful fetch
    seen_email_ids.json  : set of folder names already on disk

Each new email lives at:
    inbox/<YYYYMMDD_HHMMSS_msgidshort>/
        raw.eml          : original RFC822
        meta.json        : parsed headers (sender, subject, date, languages…)
        body.txt         : extracted text/plain (if present)
        body.html        : extracted text/html (if present)

Attachments go to ``email/attachments/<same-folder-name>/`` so the path
stays valid when the email later moves to ``processed/`` etc.
"""

from __future__ import annotations

import email
import email.header
import email.message
import email.policy
import email.utils
import imaplib
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import attachments as att_extract
from . import safe_names
from .config import Config
from .models import Email

COMMON_SENT_FOLDERS: tuple[str, ...] = (
    "[Gmail]/Sent Mail",
    "[Gmail]/Tous les messages",  # unlikely fit but covered
    "Sent",
    "Sent Items",
    "Sent Mail",
    "INBOX.Sent",
)


log = logging.getLogger(__name__)

LAST_FETCH_FILENAME = "last_fetch.txt"
SEEN_IDS_FILENAME = "seen_email_ids.json"
DEFAULT_INITIAL_HOURS = 24


# ---------- header / body / attachment parsing ----------------------------


def decode_header_value(value: str | None) -> str:
    """Decode an RFC 2047 encoded-word header into a plain string."""
    if not value:
        return ""
    parts = email.header.decode_header(value)
    out: list[str] = []
    for part, charset in parts:
        if isinstance(part, bytes):
            out.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(part)
    return "".join(out)


def parse_address_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [a.strip() for a in decode_header_value(value).split(",") if a.strip()]


def extract_email_address(value: str) -> str:
    """Pull the bare ``user@domain`` out of a possibly-decorated From/To header."""
    if not value:
        return ""
    name, addr = email.utils.parseaddr(value)
    return (addr or "").strip().lower()


def extract_display_name(value: str) -> str:
    """Pull the display name (possibly empty) out of a From header."""
    if not value:
        return ""
    name, _ = email.utils.parseaddr(value)
    return decode_header_value(name).strip()


def extract_bodies(msg: email.message.Message) -> tuple[list[str], list[str]]:
    """Walk the MIME tree and return (plain_parts, html_parts)."""
    plain: list[str] = []
    html: list[str] = []

    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        ctype = part.get_content_type()
        disposition = str(part.get("Content-Disposition", ""))
        if "attachment" in disposition:
            continue

        payload = part.get_payload(decode=True)
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except (LookupError, UnicodeDecodeError):
            text = payload.decode("utf-8", errors="replace")

        if ctype == "text/plain":
            plain.append(text)
        elif ctype == "text/html":
            html.append(text)

    return plain, html


def extract_attachments(msg: email.message.Message) -> list[dict]:
    """Return a list of ``{filename, content_type, data}`` for each attachment."""
    out: list[dict] = []
    if not msg.is_multipart():
        return out

    for part in msg.walk():
        disposition = str(part.get("Content-Disposition", ""))
        if "attachment" not in disposition and "inline" not in disposition:
            continue
        ctype = part.get_content_type()
        if ctype in ("text/plain", "text/html") and "attachment" not in disposition:
            continue

        filename = part.get_filename()
        filename = decode_header_value(filename) if filename else f"attachment.{part.get_content_subtype() or 'bin'}"
        data = part.get_payload(decode=True)
        if data:
            out.append({"filename": filename, "content_type": ctype, "data": data})
    return out


# ---------- folder naming + state -----------------------------------------


def make_email_folder_name(msg: email.message.Message) -> str:
    """Build the ``YYYYMMDD_HHMMSS_msgidshort`` folder name."""
    date_str = msg.get("Date", "") or ""
    message_id = msg.get("Message-ID", "") or ""
    try:
        dt = email.utils.parsedate_to_datetime(date_str)
        if dt is None:
            raise ValueError("parsedate returned None")
    except (TypeError, ValueError):
        dt = datetime.now(timezone.utc)
    short = re.sub(r"[^a-zA-Z0-9]", "", message_id)[:12] or "noid"
    return dt.strftime("%Y%m%d_%H%M%S") + "_" + short


def _state_path(cfg: Config, name: str) -> Path:
    return cfg.project_root / name


def get_last_fetch_timestamp(cfg: Config) -> int | None:
    p = _state_path(cfg, LAST_FETCH_FILENAME)
    if not p.exists():
        return None
    try:
        return int(p.read_text().strip())
    except (ValueError, OSError):
        return None


def save_last_fetch_timestamp(cfg: Config, ts: int) -> None:
    _state_path(cfg, LAST_FETCH_FILENAME).write_text(str(ts))


def load_seen_ids(cfg: Config) -> set[str]:
    p = _state_path(cfg, SEEN_IDS_FILENAME)
    if not p.exists():
        return set()
    try:
        return set(json.loads(p.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return set()


def save_seen_ids(cfg: Config, seen: set[str]) -> None:
    _state_path(cfg, SEEN_IDS_FILENAME).write_text(
        json.dumps(sorted(seen), ensure_ascii=False), encoding="utf-8"
    )


# ---------- writing one parsed email to disk ------------------------------


def write_email(
    cfg: Config,
    raw_email: bytes,
    *,
    target_dir: Path | None = None,
) -> tuple[Path, dict] | None:
    """Parse ``raw_email`` and write the per-email folder under ``cfg.inbox_dir``.

    Returns ``(folder_path, meta)`` on success, or ``None`` if the folder
    already existed (already-fetched).
    """
    msg = email.message_from_bytes(raw_email, policy=email.policy.compat32)
    folder_name = make_email_folder_name(msg)
    target = (target_dir or cfg.inbox_dir) / folder_name
    if target.exists():
        log.info("skip (already on disk): %s", folder_name)
        return None
    target.mkdir(parents=True, exist_ok=True)

    subject = decode_header_value(msg.get("Subject", ""))
    from_raw = msg.get("From", "")
    sender = extract_email_address(from_raw)
    sender_name = extract_display_name(from_raw)
    to_addrs = parse_address_list(msg.get("To", ""))
    cc_addrs = parse_address_list(msg.get("Cc", ""))
    bcc_addrs = parse_address_list(msg.get("Bcc", ""))
    date_str = msg.get("Date", "") or ""
    message_id = (msg.get("Message-ID", "") or "").strip()
    in_reply_to = (msg.get("In-Reply-To", "") or "").strip()
    references = [r.strip() for r in (msg.get("References", "") or "").split() if r.strip()]

    try:
        parsed_date = email.utils.parsedate_to_datetime(date_str).isoformat()
    except (TypeError, ValueError):
        parsed_date = date_str

    plain_parts, html_parts = extract_bodies(msg)
    attachments = extract_attachments(msg)

    # Extract every URL in the body. Surfaced in the recap so the user
    # can jump to links without opening the source email. Meeting URLs
    # (Meet/Zoom/Teams/etc.) are also stored separately and consumed by
    # draft_delivery to build a Google Calendar "add event" link.
    from . import url_extract as _ux  # local import; keep fetch deps light
    body_for_urls = "\n".join(plain_parts) if plain_parts else ""
    all_urls = _ux.extract_urls(body_for_urls)
    meeting_urls = [u for u in all_urls if _ux.is_meeting_url(u)]

    meta = {
        "message_id": message_id,
        "subject": subject,
        "sender": sender,
        "sender_name": sender_name,
        "to": to_addrs,
        "cc": cc_addrs,
        "bcc": bcc_addrs,
        "date": parsed_date,
        "in_reply_to": in_reply_to,
        "references": references,
        "has_attachments": bool(attachments),
        "attachment_count": len(attachments),
        "folder": folder_name,
        "urls": all_urls,
        "meeting_urls": meeting_urls,
    }

    # Persist raw + parsed artifacts.
    (target / "raw.eml").write_bytes(raw_email)
    (target / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if plain_parts:
        (target / "body.txt").write_text("\n".join(plain_parts), encoding="utf-8")
    if html_parts:
        (target / "body.html").write_text("\n".join(html_parts), encoding="utf-8")

    # Attachments: stable location keyed by folder_name, plus a relative-path
    # list under the email folder. After saving each, we extract text where
    # we can so the AI can actually read attached PDFs / docx / plain text.
    if attachments:
        att_dir = cfg.attachments_dir / folder_name
        att_dir.mkdir(parents=True, exist_ok=True)
        used: set[str] = set()
        attachment_names: list[str] = []
        for att in attachments:
            fname = safe_names.make_safe_filename(att["filename"])
            base = fname
            i = 1
            while fname in used:
                stem, dot, ext = base.rpartition(".")
                fname = f"{stem}_{i}.{ext}" if dot else f"{base}_{i}"
                i += 1
            used.add(fname)
            saved_path = att_dir / fname
            saved_path.write_bytes(att["data"])
            attachment_names.append(fname)
            try:
                att_extract.extract_and_save(saved_path)
            except Exception as exc:
                log.warning("attachment extract failed for %s: %s", saved_path, exc)
        meta["attachments"] = attachment_names
        # Re-write meta.json with the attachment list now resolved.
        (target / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    return target, meta


# ---------- loading inbox folders into Email objects ----------------------


def load_email_from_folder(folder: Path) -> Email | None:
    """Reconstruct an :class:`Email` from a previously-fetched inbox folder."""
    meta_path = folder / "meta.json"
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    body_path = folder / "body.txt"
    body_text = body_path.read_text(encoding="utf-8", errors="replace") if body_path.exists() else ""
    html_path = folder / "body.html"
    body_html = html_path.read_text(encoding="utf-8", errors="replace") if html_path.exists() else None

    try:
        date_obj: datetime | None = datetime.fromisoformat(meta.get("date") or "")
    except ValueError:
        date_obj = None

    return Email(
        message_id=meta.get("message_id", ""),
        sender=meta.get("sender", ""),
        sender_name=meta.get("sender_name", ""),
        to=list(meta.get("to") or []),
        cc=list(meta.get("cc") or []),
        subject=meta.get("subject", ""),
        date=date_obj,
        body_text=body_text,
        body_html=body_html,
        attachments=list(meta.get("attachments") or []),
        in_reply_to=meta.get("in_reply_to", ""),
        references=list(meta.get("references") or []),
    )


def iter_inbox(cfg: Config) -> list[Path]:
    """Return the per-email folders currently sitting in ``cfg.inbox_dir``."""
    if not cfg.inbox_dir.exists():
        return []
    return sorted(p for p in cfg.inbox_dir.iterdir() if p.is_dir())


# ---------- IMAP connection ------------------------------------------------


def connect_imap(cfg: Config) -> imaplib.IMAP4_SSL:
    """Open a TLS IMAP connection and authenticate. Raises on failure."""
    log.info("connecting to %s:%s as %s", cfg.imap.server, cfg.imap.port, cfg.imap.username)
    mail = imaplib.IMAP4_SSL(cfg.imap.server, cfg.imap.port)
    mail.login(cfg.imap.username, cfg.imap.password)
    log.info("logged in as %s", cfg.imap.username)
    return mail


def fetch_folder(
    mail: imaplib.IMAP4_SSL,
    folder_name: str,
    since_str: str,
    *,
    cfg: Config,
    seen: set[str],
    max_per_run: int = 0,
) -> tuple[int, int]:
    """Fetch new messages from one IMAP folder. Returns ``(new_count, error_count)``.

    ``max_per_run`` caps the number of new messages downloaded per call;
    0 = no cap.
    """
    status, _ = mail.select(_quote_mailbox(folder_name), readonly=True)
    if status != "OK":
        log.warning("could not select folder %s, skipping", folder_name)
        return 0, 0

    log.info("searching %s SINCE %s", folder_name, since_str)
    status, data = mail.search(None, f'(SINCE "{since_str}")')
    if status != "OK":
        log.error("search failed for %s: %s", folder_name, status)
        return 0, 0

    msg_nums = data[0].split()
    if not msg_nums:
        log.info("%s: 0 messages", folder_name)
        return 0, 0

    log.info("%s: %d candidate(s)", folder_name, len(msg_nums))

    msg_range = b",".join(msg_nums)
    status, header_blocks = mail.fetch(msg_range, "(BODY[HEADER.FIELDS (DATE MESSAGE-ID)])")
    if status != "OK":
        log.error("batch header fetch failed for %s", folder_name)
        return 0, 0

    new_msg_nums: list[bytes] = []
    for item in header_blocks:
        if not isinstance(item, tuple):
            continue
        msg_num_str = item[0].split()[0]
        header_msg = email.message_from_bytes(item[1], policy=email.policy.compat32)
        fname = make_email_folder_name(header_msg)
        if fname not in seen:
            new_msg_nums.append(msg_num_str)

    if max_per_run and len(new_msg_nums) > max_per_run:
        new_msg_nums = new_msg_nums[-max_per_run:]
        log.info("capping to last %d due to max_per_run", max_per_run)

    log.info("%s: %d new message(s) to download", folder_name, len(new_msg_nums))

    new_count = error_count = 0
    for msg_num in new_msg_nums:
        try:
            status, msg_data = mail.fetch(msg_num, "(RFC822)")
            if status != "OK":
                log.error("fetch failed for %s/%s", folder_name, msg_num)
                error_count += 1
                continue
            raw = msg_data[0][1]
            written = write_email(cfg, raw)
            if written:
                folder_path, meta = written
                seen.add(folder_path.name)
                new_count += 1
                log.info("saved %s: %s", folder_path.name, meta.get("subject", ""))
        except Exception as exc:
            log.error("error processing %s/%s: %s", folder_name, msg_num, exc)
            error_count += 1
    return new_count, error_count


# ---------- in-memory parsing (used by bootstrap) -------------------------


def parse_raw_to_email(raw_email: bytes) -> Email:
    """Parse RFC822 bytes into an :class:`Email` without writing to disk."""
    msg = email.message_from_bytes(raw_email, policy=email.policy.compat32)
    plain, _ = extract_bodies(msg)
    date_obj: datetime | None = None
    date_str = msg.get("Date", "") or ""
    try:
        date_obj = email.utils.parsedate_to_datetime(date_str)
    except (TypeError, ValueError):
        date_obj = None
    from_raw = msg.get("From", "") or ""
    return Email(
        message_id=(msg.get("Message-ID", "") or "").strip(),
        sender=extract_email_address(from_raw),
        sender_name=extract_display_name(from_raw),
        to=parse_address_list(msg.get("To", "")),
        cc=parse_address_list(msg.get("Cc", "")),
        subject=decode_header_value(msg.get("Subject", "")),
        date=date_obj,
        body_text="\n".join(plain),
        in_reply_to=(msg.get("In-Reply-To", "") or "").strip(),
        references=[r.strip() for r in (msg.get("References", "") or "").split() if r.strip()],
    )


def _quote_mailbox(name: str) -> str:
    """Quote an IMAP mailbox name when it contains special characters.

    Python's ``imaplib.select(name)`` passes ``name`` straight into the
    IMAP wire protocol. Gmail (and many other providers) name folders
    like ``[Gmail]/Sent Mail`` with brackets and spaces, which the IMAP
    grammar rejects unless the name is quoted. The atom rule is:
    "if it contains anything other than letters / digits / ``-`` / ``.``,
    quote it as a string." We err on the side of quoting whenever a name
    isn't pure alphanumeric.
    """
    if not name:
        return name
    if all(c.isalnum() or c in "-_." for c in name):
        return name
    # Escape embedded backslashes + double quotes per IMAP RFC 3501.
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _list_mailboxes(mail: imaplib.IMAP4_SSL) -> list[str]:
    """Return the IMAP mailbox names available on the server."""
    try:
        status, items = mail.list()
    except Exception as exc:
        log.warning("LIST failed: %s", exc)
        return []
    if status != "OK" or not items:
        return []

    available: list[str] = []
    for raw in items:
        if not raw:
            continue
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        m = re.search(r'"([^"]+)"\s*$', text)
        if m:
            available.append(m.group(1))
        else:
            available.append(text.split()[-1])
    return available


def detect_sent_folder(mail: imaplib.IMAP4_SSL) -> str | None:
    """Best-effort discovery of the IMAP folder name that holds sent items."""
    available = _list_mailboxes(mail)
    if not available:
        return None
    for candidate in COMMON_SENT_FOLDERS:
        if candidate in available:
            return candidate
    for name in available:
        if "Sent" in name or "sent" in name or "Envoy" in name:
            return name
    return None


def fetch_into_memory(
    cfg: Config,
    *,
    folder: str = "INBOX",
    days: int = 30,
) -> list[Email]:
    """Fetch the last ``days`` of email from ``folder`` without writing to disk.

    Used by bootstrap to scan history into Email objects for analysis.
    """
    mail = connect_imap(cfg)
    out: list[Email] = []
    try:
        status, _ = mail.select(_quote_mailbox(folder), readonly=True)
        if status != "OK":
            log.warning("could not select %s for in-memory fetch", folder)
            return []
        since_dt = datetime.now(timezone.utc) - timedelta(days=days)
        since_str = since_dt.strftime("%d-%b-%Y")
        status, data = mail.search(None, f'(SINCE "{since_str}")')
        if status != "OK":
            return []
        nums = data[0].split()
        for n in nums:
            try:
                status, msg_data = mail.fetch(n, "(RFC822)")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                if not isinstance(raw, (bytes, bytearray)):
                    continue
                out.append(parse_raw_to_email(raw))
            except Exception as exc:
                log.warning("fetch %s failed: %s", n, exc)
        return out
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def fetch_new_emails(
    cfg: Config,
    *,
    folders: tuple[str, ...] = ("INBOX",),
    since_days: int | None = None,
    max_per_run: int | None = None,
) -> dict:
    """Top-level fetch: connect, walk folders, write emails, persist state.

    Returns ``{"new": N, "errors": M, "since": "<imap-date>"}``.
    """
    cfg.inbox_dir.mkdir(parents=True, exist_ok=True)
    cfg.attachments_dir.mkdir(parents=True, exist_ok=True)

    mail = connect_imap(cfg)
    try:
        if since_days is not None:
            since_ts = int(time.time()) - since_days * 86400
        else:
            last = get_last_fetch_timestamp(cfg)
            since_ts = last if last else int(time.time()) - DEFAULT_INITIAL_HOURS * 3600
        since_dt = datetime.fromtimestamp(since_ts, tz=timezone.utc)
        since_str = since_dt.strftime("%d-%b-%Y")

        seen = load_seen_ids(cfg)
        fetch_started = int(time.time())
        cap = max_per_run if max_per_run is not None else cfg.pipeline.max_per_run

        total_new = total_err = 0
        for f in folders:
            try:
                n, e = fetch_folder(mail, f, since_str, cfg=cfg, seen=seen, max_per_run=cap)
                total_new += n
                total_err += e
            except Exception as exc:
                log.error("folder %s failed: %s", f, exc)
                total_err += 1
            save_seen_ids(cfg, seen)

        save_last_fetch_timestamp(cfg, fetch_started)
        return {"new": total_new, "errors": total_err, "since": since_str}
    finally:
        try:
            mail.logout()
        except Exception:
            pass
