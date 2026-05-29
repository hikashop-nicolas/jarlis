"""Draft delivery: email the AI-drafted reply to the user's notification address.

Goal: the user keeps their normal email-client habits. JARLIS mails them
a notification containing the proposed reply text (and the original
email it answers) at ``[notification].to``. The user reads it in their
normal inbox, then sends the real reply from wherever they prefer
(webmail, phone, desktop client) using the draft as a starting point.

The local copy under ``waiting_for_approval/<slug>.md`` is also kept,
unconditionally, as an audit trail.

Modes (``[drafts].mode``):
  - ``email`` (default): write the local file AND send the notification.
  - ``file``           : write the local file only.
"""

from __future__ import annotations

import logging
from email.message import EmailMessage
from pathlib import Path

from . import attachments as att_extract
from . import i18n, memory, notify, text_cleanup, url_extract
from .config import Config
from .models import Classification, Email

log = logging.getLogger(__name__)

# How much of the original email body to quote into the notification.
ORIGINAL_QUOTE_LIMIT = 4000


def deliver_draft(
    cfg: Config,
    email_obj: Email,
    cls: Classification,
    draft_text: str,
    *,
    local_path: Path | None = None,
    original_translation: str | None = None,
    draft_translation: str | None = None,
    summary: str | None = None,
    original_lang: str | None = None,
    draft_lang: str | None = None,
    user_lang: str | None = None,
    attachment_paths: list[Path] | None = None,
) -> bool:
    """Run the configured delivery step on top of the always-on local file.

    Returns True if the email notification was sent (or wasn't requested),
    False if it was requested but failed.

    The optional ``original_translation`` / ``draft_translation`` / ``summary``
    are produced by :mod:`jarlis.translation` upstream and shown in the
    notification body alongside the originals.
    """
    mode = (cfg.drafts.mode or "email").lower()
    if mode == "file":
        return True
    if mode != "email":
        log.warning("unknown drafts.mode %r; defaulting to 'email'", mode)
        mode = "email"

    return _send_notification(
        cfg, email_obj, cls, draft_text, local_path,
        original_translation=original_translation,
        draft_translation=draft_translation,
        summary=summary,
        original_lang=original_lang,
        draft_lang=draft_lang,
        user_lang=user_lang,
        attachment_paths=attachment_paths,
    )


def deliver_attention(
    cfg: Config,
    email_obj: Email,
    cls: Classification,
    *,
    local_folder: Path | None = None,
    attachment_paths: list[Path] | None = None,
) -> bool:
    """Email the user about a ``flagged`` email (needs attention, no draft).

    Mirrors the draft notification minus the proposed reply: flagged mail
    warrants the user's eyes, not an auto-draft. Honors ``[drafts].mode``
    ('file' keeps it to ``pending_attention.md`` only). Returns True when
    the notification was sent (or wasn't requested), False on send failure.
    """
    mode = (cfg.drafts.mode or "email").lower()
    if mode == "file":
        return True
    if not cfg.notification.to:
        log.warning("flagged notification skipped: [notification].to is unset")
        return False

    lang = (cfg.user.languages or ["en"])[0]
    org = cfg.organization.name or "JARLIS"
    name = cfg.user.firstname or ""

    subject_prefix = i18n.t("attention.notification.subject_prefix", lang, org=org)
    subject = f"{subject_prefix}: {email_obj.subject or '(no subject)'}"

    body_text = _format_attention_body(
        cfg, email_obj, cls,
        lang=lang, name=name,
        local_folder=local_folder,
        attachment_paths=attachment_paths,
    )
    return _send_with_reply_to(
        cfg,
        recipient=cfg.notification.to,
        subject=subject,
        body_text=body_text,
        reply_to=email_obj.sender,
    )


def _format_attention_body(
    cfg: Config,
    email_obj: Email,
    cls: Classification,
    *,
    lang: str,
    name: str,
    local_folder: Path | None = None,
    attachment_paths: list[Path] | None = None,
) -> str:
    """Plain-text body for a flagged-email notification.

    Layout mirrors the draft notification (reason, original header, cleaned
    body, meeting calendar link) but omits the proposed-reply and
    translation sections, which don't apply to flagged mail.
    """
    greeting = i18n.t("drafts.notification.greeting", lang, name=name) if name else ""
    intro = i18n.t(
        "attention.notification.intro",
        lang,
        from_address=cfg.user.email or cfg.imap.username or "your mailbox",
    )
    label_original = i18n.t("drafts.notification.label_original", lang)
    label_reason = i18n.t("drafts.notification.label_reason", lang) if cls.reason else ""
    label_attachments = i18n.t("drafts.notification.label_attachments", lang)

    sender_display = (
        f"{email_obj.sender_name} <{email_obj.sender}>"
        if email_obj.sender_name
        else email_obj.sender
    )
    date = email_obj.date.isoformat() if email_obj.date else "(unknown date)"

    domain_footers = text_cleanup.load_footers(memory.auto_footers_path(cfg))
    domain_footer = domain_footers.get(text_cleanup.domain_of(email_obj.sender))
    cleaned_body = text_cleanup.clean_body(
        email_obj.body_text or "", domain_footer=domain_footer,
    )
    truncated_marker = ""
    if len(cleaned_body) > ORIGINAL_QUOTE_LIMIT:
        cleaned_body = cleaned_body[:ORIGINAL_QUOTE_LIMIT]
        truncated_marker = "[... truncated by JARLIS ...]"

    parts: list[str] = []
    if greeting:
        parts.append(greeting)
        parts.append("")
    parts.append(intro)
    parts.append("")
    if label_reason:
        parts.append(f"({label_reason}: {cls.reason})")
        parts.append("")

    parts.append(f"--- {label_original} ---")
    parts.append(f"From:    {sender_display}")
    if email_obj.to:
        parts.append(f"To:      {', '.join(email_obj.to)}")
    if email_obj.cc:
        parts.append(f"Cc:      {', '.join(email_obj.cc)}")
    parts.append(f"Date:    {date}")
    parts.append(f"Subject: {email_obj.subject}")
    if email_obj.attachments:
        parts.append(f"{label_attachments}:")
        if attachment_paths:
            parts.extend(att_extract.render_for_notification(attachment_paths))
        else:
            for att_name in email_obj.attachments:
                parts.append(f"  {att_name}")
    parts.append("")
    parts.append(cleaned_body or "(no readable body after stripping quotes)")
    if truncated_marker:
        parts.append(truncated_marker)

    # Flagged mail is often scheduling: surface a one-click calendar link.
    meeting_urls = url_extract.extract_meeting_urls(email_obj.body_text or "")
    if meeting_urls:
        dt_meeting = url_extract.parse_meeting_datetime(email_obj.body_text or "")
        meet_url = meeting_urls[0]
        calendar_url = url_extract.build_google_calendar_url(
            title=email_obj.subject or "Meeting",
            details=f"{meet_url}\n\n(via JARLIS)",
            dt_start=dt_meeting,
        )
        parts.append("")
        parts.append(i18n.t("drafts.notification.label_meeting", lang))
        parts.append(f"  {meet_url}")
        when_line = i18n.t(
            "drafts.notification.meeting_when_detected" if dt_meeting
            else "drafts.notification.meeting_when_unknown",
            lang,
            when=dt_meeting.strftime("%Y-%m-%d %H:%M") if dt_meeting else "",
        )
        parts.append(f"  {when_line}")
        parts.append(f"  {i18n.t('drafts.notification.calendar_link', lang, url=calendar_url)}")

    if local_folder is not None:
        try:
            rel = local_folder.relative_to(cfg.project_root)
        except ValueError:
            rel = local_folder
        parts.append("")
        parts.append(i18n.t("drafts.notification.local_path", lang, path=str(rel)))
    parts.append("")
    parts.append(i18n.t("drafts.notification.signoff", lang))

    return "\n".join(parts)


def _send_notification(
    cfg: Config,
    email_obj: Email,
    cls: Classification,
    draft_text: str,
    local_path: Path | None,
    *,
    original_translation: str | None,
    draft_translation: str | None,
    summary: str | None,
    original_lang: str | None,
    draft_lang: str | None,
    user_lang: str | None,
    attachment_paths: list[Path] | None = None,
) -> bool:
    """Send a normal email to ``[notification].to`` containing the draft."""
    if not cfg.notification.to:
        log.warning("drafts.mode='email' but [notification].to is unset; skipping")
        return False

    lang = (cfg.user.languages or ["en"])[0]
    org = cfg.organization.name or "JARLIS"
    name = cfg.user.firstname or ""

    subject_prefix = i18n.t("drafts.notification.subject_prefix", lang, org=org)
    subject = f"{subject_prefix}: {email_obj.subject or '(no subject)'}"

    body_text = _format_notification_body(
        cfg, email_obj, cls, draft_text, local_path,
        lang=lang, name=name,
        original_translation=original_translation,
        draft_translation=draft_translation,
        summary=summary,
        original_lang=original_lang,
        draft_lang=draft_lang,
        user_lang=user_lang,
        attachment_paths=attachment_paths,
    )

    return _send_with_reply_to(
        cfg,
        recipient=cfg.notification.to,
        subject=subject,
        body_text=body_text,
        reply_to=email_obj.sender,
    )


def _format_notification_body(
    cfg: Config,
    email_obj: Email,
    cls: Classification,
    draft_text: str,
    local_path: Path | None,
    *,
    lang: str,
    name: str,
    original_translation: str | None = None,
    draft_translation: str | None = None,
    summary: str | None = None,
    original_lang: str | None = None,
    draft_lang: str | None = None,
    user_lang: str | None = None,
    attachment_paths: list[Path] | None = None,
) -> str:
    """Build the plain-text body of the notification email.

    Layout:
        Greeting (if name known)
        Intro paragraph

        --- Original email ---
        From / To / Cc / Date / Subject
        Attachments (if any)

        <full original body, truncated only if exceeds ORIGINAL_QUOTE_LIMIT>

        --- Proposed draft ---

        <draft text>

        (Reason: ...)
        Local copy: ...

        Signoff
    """
    greeting = i18n.t("drafts.notification.greeting", lang, name=name) if name else ""
    intro = i18n.t(
        "drafts.notification.intro",
        lang,
        from_address=cfg.user.email or cfg.imap.username or "your mailbox",
    )
    label_original = i18n.t("drafts.notification.label_original", lang)
    label_draft = i18n.t("drafts.notification.label_draft", lang)
    label_reason = i18n.t("drafts.notification.label_reason", lang) if cls.reason else ""
    label_attachments = i18n.t("drafts.notification.label_attachments", lang)
    label_translation_of_original = i18n.t(
        "drafts.notification.label_translation_original",
        lang, source_lang=original_lang or "?", target_lang=user_lang or lang,
    )
    label_translation_of_draft = i18n.t(
        "drafts.notification.label_translation_draft",
        lang, source_lang=draft_lang or "?", target_lang=user_lang or lang,
    )
    label_summary = i18n.t("drafts.notification.label_summary", lang)
    sign = i18n.t("drafts.notification.signoff", lang)

    sender_display = (
        f"{email_obj.sender_name} <{email_obj.sender}>"
        if email_obj.sender_name
        else email_obj.sender
    )
    date = email_obj.date.isoformat() if email_obj.date else "(unknown date)"

    # Strip quoted history + per-domain footer for display. Translations and
    # summaries upstream were already computed against the cleaned version.
    domain_footers = text_cleanup.load_footers(memory.auto_footers_path(cfg))
    domain_footer = domain_footers.get(text_cleanup.domain_of(email_obj.sender))
    cleaned_body = text_cleanup.clean_body(
        email_obj.body_text or "", domain_footer=domain_footer,
    )
    truncated_marker = ""
    if len(cleaned_body) > ORIGINAL_QUOTE_LIMIT:
        cleaned_body = cleaned_body[:ORIGINAL_QUOTE_LIMIT]
        truncated_marker = "[... truncated by JARLIS ...]"

    parts: list[str] = []
    if greeting:
        parts.append(greeting)
        parts.append("")
    parts.append(intro)
    parts.append("")

    # Summary first: lets the user grasp the email at a glance, then read
    # the cleaned original, then the translation if any.
    if summary:
        parts.append(f"--- {label_summary} ---")
        parts.append("")
        parts.append(summary.strip())
        parts.append("")

    parts.append(f"--- {label_original} ---")
    parts.append(f"From:    {sender_display}")
    if email_obj.to:
        parts.append(f"To:      {', '.join(email_obj.to)}")
    if email_obj.cc:
        parts.append(f"Cc:      {', '.join(email_obj.cc)}")
    parts.append(f"Date:    {date}")
    parts.append(f"Subject: {email_obj.subject}")
    if email_obj.attachments:
        parts.append(f"{label_attachments}:")
        if attachment_paths:
            # Full paths + extracted-text previews so the user can open them.
            parts.extend(att_extract.render_for_notification(attachment_paths))
        else:
            for name in email_obj.attachments:
                parts.append(f"  {name}")
    parts.append("")
    parts.append(cleaned_body or "(no readable body after stripping quotes)")
    if truncated_marker:
        parts.append(truncated_marker)

    # Meeting URLs → one-click Google Calendar "Add event" link. The full
    # email body is already in the notification, so we DON'T list every
    # URL here (would just duplicate). Calendar links are the value-add.
    from . import url_extract as _ux
    meeting_urls = _ux.extract_meeting_urls(email_obj.body_text or "")
    if meeting_urls:
        dt_meeting = _ux.parse_meeting_datetime(email_obj.body_text or "")
        meet_url = meeting_urls[0]  # first one is usually the relevant one
        calendar_url = _ux.build_google_calendar_url(
            title=email_obj.subject or "Meeting",
            details=f"{meet_url}\n\n(via JARLIS)",
            dt_start=dt_meeting,
        )
        parts.append("")
        parts.append(i18n.t("drafts.notification.label_meeting", lang))
        parts.append(f"  {meet_url}")
        when_line = i18n.t(
            "drafts.notification.meeting_when_detected" if dt_meeting
            else "drafts.notification.meeting_when_unknown",
            lang,
            when=dt_meeting.strftime("%Y-%m-%d %H:%M") if dt_meeting else "",
        )
        parts.append(f"  {when_line}")
        parts.append(f"  {i18n.t('drafts.notification.calendar_link', lang, url=calendar_url)}")

    if original_translation:
        parts.append("")
        parts.append(f"--- {label_translation_of_original} ---")
        parts.append("")
        parts.append(original_translation.strip())
    parts.append("")
    parts.append(f"--- {label_draft} ---")
    parts.append("")
    parts.append(draft_text.strip())
    if draft_translation:
        parts.append("")
        parts.append(f"--- {label_translation_of_draft} ---")
        parts.append("")
        parts.append(draft_translation.strip())
    if label_reason:
        parts.append("")
        parts.append(f"({label_reason}: {cls.reason})")
    if local_path is not None:
        try:
            rel = local_path.relative_to(cfg.project_root)
        except ValueError:
            rel = local_path
        parts.append("")
        parts.append(i18n.t("drafts.notification.local_path", lang, path=str(rel)))
    parts.append("")
    parts.append(sign)

    return "\n".join(parts)


def _send_with_reply_to(
    cfg: Config,
    *,
    recipient: str,
    subject: str,
    body_text: str,
    reply_to: str,
) -> bool:
    """Like ``notify.send_email`` but also sets ``Reply-To`` on the message.

    Done here (rather than as a flag on ``notify.send_email``) because
    the Reply-To wiring is specific to the draft-notification flow.
    """
    if not cfg.smtp.server:
        log.warning("no SMTP server configured; cannot deliver draft notification")
        return False

    msg = EmailMessage()
    msg["From"] = notify._from_address(cfg)
    msg["To"] = recipient
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body_text)

    log.info("draft notification: %r to %s (reply-to=%s)", subject, recipient, reply_to)
    try:
        import smtplib

        with smtplib.SMTP(cfg.smtp.server, cfg.smtp.port, timeout=30) as smtp:
            smtp.ehlo()
            try:
                smtp.starttls()
                smtp.ehlo()
            except smtplib.SMTPNotSupportedError:
                log.warning("server does not support STARTTLS; sending unencrypted")
            user = cfg.smtp.username or cfg.imap.username
            pw = cfg.smtp.password or cfg.imap.password
            if user and pw:
                smtp.login(user, pw)
            smtp.send_message(msg)
        return True
    except Exception as exc:
        log.error("draft notification send failed: %s", exc)
        return False
