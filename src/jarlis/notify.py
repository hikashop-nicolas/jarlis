"""SMTP notifier.

Sends plain-text emails via the configured SMTP server. Used by:

  - :mod:`jarlis.recap`    : daily/weekly recap emails
  - :mod:`jarlis.pipeline` : stuck-pipeline alerts
  - bootstrap / setup wizard: test ping after configuration

All user-facing copy goes through :mod:`jarlis.i18n` so strings can be
translated. Operator log messages stay in English.
"""

from __future__ import annotations

import logging
import re
import smtplib
from email.message import EmailMessage
from pathlib import Path

from . import i18n
from .config import Config

log = logging.getLogger(__name__)

_HEADER_NEWLINE_RE = re.compile(r"\s*[\r\n]+\s*")


def _sanitize_header(value: str) -> str:
    """Flatten CR/LF (and surrounding whitespace) to a single space.

    EmailMessage rejects header values containing line breaks (a header
    injection guard). IMAP subjects sometimes arrive with a folded
    continuation that keeps its newline (e.g. "...traitement de\n paie"),
    which would otherwise crash the send. Used for Subject/To/Reply-To.
    """
    return _HEADER_NEWLINE_RE.sub(" ", value or "").strip()


def _from_address(cfg: Config) -> str:
    """Pick the From: header for outgoing JARLIS notifications."""
    return cfg.smtp.username or cfg.imap.username or cfg.user.email or ""


def _password(cfg: Config) -> str:
    return cfg.smtp.password or cfg.imap.password


def send_email(
    cfg: Config,
    subject: str,
    body_text: str,
    *,
    recipient: str | None = None,
) -> bool:
    """Send a plain-text email. Returns True on success, False on failure.

    Errors are logged, not raised: callers (pipeline, recap) shouldn't
    crash because notifications are flaky.
    """
    recipient = recipient or cfg.notification.to
    if not recipient:
        log.warning("no notification.to configured; skipping send")
        return False
    if not cfg.smtp.server:
        log.warning("no SMTP server configured; skipping send")
        return False

    msg = EmailMessage()
    msg["From"] = _from_address(cfg)
    msg["To"] = _sanitize_header(recipient)
    msg["Subject"] = _sanitize_header(subject)
    msg.set_content(body_text)

    log.info("smtp send: %r to %s via %s:%s", subject, recipient, cfg.smtp.server, cfg.smtp.port)
    try:
        with smtplib.SMTP(cfg.smtp.server, cfg.smtp.port, timeout=30) as smtp:
            smtp.ehlo()
            try:
                smtp.starttls()
                smtp.ehlo()
            except smtplib.SMTPNotSupportedError:
                log.warning("server does not support STARTTLS; sending unencrypted")
            user = cfg.smtp.username or cfg.imap.username
            pw = _password(cfg)
            if user and pw:
                smtp.login(user, pw)
            smtp.send_message(msg)
        return True
    except (smtplib.SMTPException, OSError) as exc:
        log.error("smtp send failed: %s", exc)
        return False


# ---------- canned messages ----------------------------------------------


def send_test_ping(cfg: Config) -> bool:
    lang = (cfg.user.languages or ["en"])[0]
    subject = i18n.t("notify.test.subject", lang, org=cfg.organization.name or "JARLIS")
    body = i18n.t("notify.test.body", lang, name=cfg.user.firstname or "there")
    return send_email(cfg, subject, body)


def send_overlap_alert(cfg: Config, *, decision, label: str = "pipeline") -> bool:
    """Tell the user that two runs of ``label`` overlapped.

    Two shapes: we killed the previous run and carried on, or we had already
    killed one last time and this run stood down instead.
    """
    from .runlock import ABORTED

    lang = (cfg.user.languages or ["en"])[0]
    key = "notify.overlap_aborted" if decision.action == ABORTED else "notify.overlap_killed"
    org = cfg.organization.name or "JARLIS"
    subject = i18n.t(f"{key}.subject", lang, org=org, label=label)
    body = i18n.t(
        f"{key}.body",
        lang,
        name=cfg.user.firstname or "there",
        label=label,
        pid=decision.previous_pid or "?",
        started_at=decision.previous_started_at or "?",
        log_path=str(cfg.project_root / f"com.jarlis.{label}.log"),
    )
    return send_email(cfg, subject, body)


def send_timeout_alert(cfg: Config, *, label: str = "pipeline", seconds: int) -> bool:
    """Tell the user that a run was killed by the watchdog."""
    lang = (cfg.user.languages or ["en"])[0]
    subject = i18n.t(
        "notify.timeout.subject",
        lang,
        org=cfg.organization.name or "JARLIS",
        label=label,
    )
    body = i18n.t(
        "notify.timeout.body",
        lang,
        name=cfg.user.firstname or "there",
        label=label,
        minutes=max(1, seconds // 60),
        log_path=str(cfg.project_root / f"com.jarlis.{label}.log"),
    )
    return send_email(cfg, subject, body)


def send_stuck_alert(
    cfg: Config,
    *,
    count: int,
    threshold: int,
    history: list[int],
    log_path: Path | str,
) -> bool:
    lang = (cfg.user.languages or ["en"])[0]
    subject = i18n.t(
        "notify.stuck.subject",
        lang,
        org=cfg.organization.name or "JARLIS",
        count=count,
    )
    body = i18n.t(
        "notify.stuck.body",
        lang,
        name=cfg.user.firstname or "there",
        count=count,
        threshold=threshold,
        history=", ".join(str(h) for h in history),
        log_path=str(log_path),
    )
    return send_email(cfg, subject, body)
