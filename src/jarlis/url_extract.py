"""URL extraction + meeting calendar helpers.

Two responsibilities, both useful to surface in the recap and the
draft-notification email:

  1. Pull every ``http(s)://…`` URL out of an email body and deduplicate
     in first-seen order. The recap renderer lists these so the user can
     act on links without opening the source email.

  2. When a URL points to a known video-conferencing host (Google Meet,
     Zoom, Teams, Webex, Whereby, Jitsi), recognize it as a meeting URL
     and build a one-click Google Calendar template URL pre-filled with
     the title, the meeting link, and (best effort) the date/time the
     email mentions for the meeting.

Date parsing is intentionally best-effort: a few common Japanese and
French date formats are recognized; everything else degrades to a
calendar URL with no ``dates`` field, leaving the user to fill in the
time in the Google Calendar UI.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from urllib.parse import quote_plus, urlparse


_URL_RE = re.compile(r"https?://[^\s<>\"'\\)\]\}]+")
# Punctuation that is almost always glued onto a URL in prose rather than
# part of the URL itself. Stripped greedily off the tail.
_TRAILING_PUNCT_RE = re.compile(r"[.,;:!?\)\]\}>'\"]+$")


def extract_urls(text: str) -> list[str]:
    """Return ordered, unique HTTP(S) URLs found in ``text``."""
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _URL_RE.finditer(text):
        url = _TRAILING_PUNCT_RE.sub("", m.group(0))
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


_MEETING_HOSTS: tuple[str, ...] = (
    "meet.google.com",
    "zoom.us",
    "zoom.com",
    "teams.microsoft.com",
    "teams.live.com",
    "webex.com",
    "whereby.com",
    "meet.jit.si",
)


def is_meeting_url(url: str) -> bool:
    """True if ``url`` points to a known video-conferencing service."""
    u = (url or "").lower()
    return any(host in u for host in _MEETING_HOSTS)


def extract_meeting_urls(text: str) -> list[str]:
    """Return only the URLs from ``text`` that look like meeting links."""
    return [u for u in extract_urls(text) if is_meeting_url(u)]


# ---------- noise filtering (for the recap link list) --------------------


# Substrings that mark a URL as boilerplate rather than actionable content:
# listserv message permalinks, opt-out/unsubscribe controls, and AV-scanner
# signature links. A single committee thread routinely carries 20+ of these,
# which bury the real links (docs, drive, meetings, forms) in the recap.
_NOISE_URL_SUBSTRINGS: tuple[str, ...] = (
    "groups.google.com/d/msgid",
    "groups.google.com/d/optout",
    "gaggle.email",
    "avast.com/sig-email",
    "utm_campaign=sig-email",
    "/unsubscribe",
    "/replytosender",
)


def is_noise_url(url: str) -> bool:
    """True if ``url`` is boilerplate (listserv/footer/unsubscribe/signature).

    Also treats bare homepage links (scheme + host, no path or query) as
    noise: those are almost always the org's signature link, not content.
    """
    u = (url or "").lower()
    if any(s in u for s in _NOISE_URL_SUBSTRINGS):
        return True
    try:
        p = urlparse(u)
    except ValueError:
        return False
    return p.netloc != "" and p.path in ("", "/") and not p.query


def filter_display_urls(
    urls: list[str], *, limit: int | None = 8,
) -> tuple[list[str], int]:
    """Drop boilerplate URLs and cap the rest for display.

    Returns ``(shown, hidden_count)`` where ``hidden_count`` is the number
    of non-noise URLs trimmed by ``limit`` (0 when nothing was trimmed).
    Noise URLs are dropped silently and never counted.
    """
    kept = [u for u in (urls or []) if not is_noise_url(u)]
    if limit is not None and len(kept) > limit:
        return kept[:limit], len(kept) - limit
    return kept, 0


# ---------- date / time parsing -----------------------------------------


# Japanese long form: ``2026年5月20日(水) 15:00`` (year + weekday + time optional).
_JP_DATE_LONG_RE = re.compile(
    r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
    r"(?:\s*\([^)]*\))?"
    r"(?:\s*(\d{1,2})\s*[:時]\s*(\d{1,2})?)?"
)

# Japanese short form (no year, infer current/next year): ``5月20日(水) 15:00``.
_JP_DATE_SHORT_RE = re.compile(
    r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日"
    r"(?:\s*\([^)]*\))?"
    r"(?:\s*(\d{1,2})\s*[:時]\s*(\d{1,2})?)?"
)

_FR_MONTHS: dict[str, int] = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
    "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
}

# French: ``mardi 19 mai à 13h30`` / ``19 mai 2026 à 13h`` / ``mardi 19/05 à 13h``.
_FR_DATE_NAMED_RE = re.compile(
    r"(?<![\w-])(\d{1,2})\s+"
    r"(janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|"
    r"septembre|octobre|novembre|décembre|decembre)"
    r"(?:\s+(\d{4}))?"
    r"(?:[^\d\n]{0,30}?(\d{1,2})\s*[h:](\d{2})?)?",
    re.IGNORECASE,
)


def _infer_year(month: int, day: int, today: date) -> int:
    """For year-less dates, pick the year that puts the date in the near future.

    If the (month, day) has already passed in ``today.year``, roll to next year.
    Generous tolerance: "today" itself counts as still upcoming.
    """
    candidate = date(today.year, month, day)
    if candidate >= today:
        return today.year
    return today.year + 1


def parse_meeting_datetime(text: str, *, today: date | None = None) -> datetime | None:
    """Best-effort extraction of a meeting date/time from ``text``.

    Tries Japanese (``YYYY年M月D日 HH:MM``, ``M月D日 HH:MM``) and French
    (``D mois YYYY à HHhMM``) formats in that order. Returns a naive
    :class:`datetime` (treated as user's local time) or ``None``.

    When the time is missing, defaults to 09:00 to give the calendar
    event a sensible placeholder the user can adjust.
    """
    if not text:
        return None
    today = today or date.today()

    m = _JP_DATE_LONG_RE.search(text)
    if m:
        try:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            hh = int(m.group(4)) if m.group(4) else 9
            mm = int(m.group(5)) if m.group(5) else 0
            return datetime(y, mo, d, hh, mm)
        except (ValueError, TypeError):
            pass

    m = _JP_DATE_SHORT_RE.search(text)
    if m:
        try:
            mo, d = int(m.group(1)), int(m.group(2))
            hh = int(m.group(3)) if m.group(3) else 9
            mm = int(m.group(4)) if m.group(4) else 0
            y = _infer_year(mo, d, today)
            return datetime(y, mo, d, hh, mm)
        except (ValueError, TypeError):
            pass

    m = _FR_DATE_NAMED_RE.search(text)
    if m:
        try:
            d = int(m.group(1))
            mo = _FR_MONTHS[m.group(2).lower()]
            y = int(m.group(3)) if m.group(3) else _infer_year(mo, d, today)
            hh = int(m.group(4)) if m.group(4) else 9
            mm = int(m.group(5)) if m.group(5) else 0
            return datetime(y, mo, d, hh, mm)
        except (ValueError, KeyError, TypeError):
            pass

    return None


# ---------- Google Calendar template URL --------------------------------


def build_google_calendar_url(
    *,
    title: str,
    details: str = "",
    dt_start: datetime | None = None,
    duration_minutes: int = 60,
) -> str:
    """Return a Google Calendar ``Add event`` template URL.

    When ``dt_start`` is provided, encodes a ``dates=…/…`` range using
    ``duration_minutes`` (default 60). When ``None``, the URL still
    pre-fills ``text`` and ``details`` so the user can pick the date
    manually in the Google Calendar UI.
    """
    params = ["action=TEMPLATE", f"text={quote_plus(title or '')}"]
    if details:
        params.append(f"details={quote_plus(details)}")
    if dt_start:
        dt_end = dt_start + timedelta(minutes=duration_minutes)
        params.append(f"dates={dt_start:%Y%m%dT%H%M%S}/{dt_end:%Y%m%dT%H%M%S}")
    return "https://calendar.google.com/calendar/render?" + "&".join(params)
