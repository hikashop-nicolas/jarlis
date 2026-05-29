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
import smtplib
from email.message import EmailMessage
from pathlib import Path

from . import i18n
from .config import Config

log = logging.getLogger(__name__)


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
    msg["To"] = recipient
    msg["Subject"] = subject
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
