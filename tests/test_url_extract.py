"""Tests for url_extract: URL extraction, meeting detection, date parsing,
Google Calendar URL construction."""

from __future__ import annotations

from datetime import date, datetime
from urllib.parse import parse_qs, urlsplit

from jarlis import url_extract


# ---------- extract_urls --------------------------------------------------


def test_extract_urls_basic() -> None:
    text = "Voir https://example.com/a et https://example.com/b pour les détails."
    assert url_extract.extract_urls(text) == [
        "https://example.com/a",
        "https://example.com/b",
    ]


def test_extract_urls_strips_trailing_punctuation() -> None:
    text = "Va sur https://example.com/path. Et lis https://example.com/end)."
    out = url_extract.extract_urls(text)
    assert out == ["https://example.com/path", "https://example.com/end"]


def test_extract_urls_dedups() -> None:
    text = "https://x.example https://x.example https://y.example"
    assert url_extract.extract_urls(text) == ["https://x.example", "https://y.example"]


def test_extract_urls_empty_inputs() -> None:
    assert url_extract.extract_urls("") == []
    assert url_extract.extract_urls(None) == []  # type: ignore[arg-type]


# ---------- meeting detection --------------------------------------------


def test_is_meeting_url_recognizes_common_hosts() -> None:
    assert url_extract.is_meeting_url("https://meet.google.com/abc-defg-hij")
    assert url_extract.is_meeting_url("https://us02web.zoom.us/j/1234567890")
    assert url_extract.is_meeting_url("https://teams.microsoft.com/l/meetup-join/...")
    assert url_extract.is_meeting_url("https://meet.jit.si/MyRoom")
    assert not url_extract.is_meeting_url("https://example.com/")
    assert not url_extract.is_meeting_url("")


def test_extract_meeting_urls_filters() -> None:
    text = (
        "Hi! Join us at https://meet.google.com/abc-defg-hij — "
        "see also https://example.com/agenda for the program."
    )
    out = url_extract.extract_meeting_urls(text)
    assert out == ["https://meet.google.com/abc-defg-hij"]


# ---------- date parsing -------------------------------------------------


def test_parse_meeting_datetime_japanese_long_form() -> None:
    text = "次回は2026年5月20日(水) 15:00から会議室Aで行います。"
    dt = url_extract.parse_meeting_datetime(text)
    assert dt == datetime(2026, 5, 20, 15, 0)


def test_parse_meeting_datetime_japanese_short_form_infers_year() -> None:
    """Year-less ``M月D日`` should be the next upcoming occurrence."""
    text = "次回は5月20日(水) 15:00です。"
    dt = url_extract.parse_meeting_datetime(text, today=date(2026, 5, 14))
    assert dt == datetime(2026, 5, 20, 15, 0)


def test_parse_meeting_datetime_japanese_short_form_rolls_to_next_year() -> None:
    text = "次回は1月10日(月) 10:00です。"
    dt = url_extract.parse_meeting_datetime(text, today=date(2026, 5, 14))
    assert dt == datetime(2027, 1, 10, 10, 0)


def test_parse_meeting_datetime_french_named_month() -> None:
    text = "Rendez-vous mardi 19 mai 2026 à 13h30."
    dt = url_extract.parse_meeting_datetime(text)
    assert dt == datetime(2026, 5, 19, 13, 30)


def test_parse_meeting_datetime_french_no_year_no_minutes() -> None:
    text = "On se voit le 19 mai à 13h."
    dt = url_extract.parse_meeting_datetime(text, today=date(2026, 5, 14))
    assert dt == datetime(2026, 5, 19, 13, 0)


def test_parse_meeting_datetime_returns_none_when_no_date() -> None:
    assert url_extract.parse_meeting_datetime("rien de spécial dans ce texte") is None
    assert url_extract.parse_meeting_datetime("") is None


# ---------- Google Calendar URL builder ----------------------------------


def test_build_google_calendar_url_with_dates() -> None:
    url = url_extract.build_google_calendar_url(
        title="Réunion AF",
        details="Meet: https://meet.google.com/x",
        dt_start=datetime(2026, 5, 19, 13, 0),
        duration_minutes=60,
    )
    parts = urlsplit(url)
    assert parts.scheme == "https"
    assert parts.netloc == "calendar.google.com"
    qs = parse_qs(parts.query)
    assert qs["action"] == ["TEMPLATE"]
    assert qs["text"] == ["Réunion AF"]
    assert "Meet" in qs["details"][0]
    assert qs["dates"] == ["20260519T130000/20260519T140000"]


def test_build_google_calendar_url_without_dates() -> None:
    url = url_extract.build_google_calendar_url(title="No date yet", details="")
    assert "dates=" not in url
    assert "text=No+date+yet" in url


def test_build_google_calendar_url_url_encodes_special_chars() -> None:
    url = url_extract.build_google_calendar_url(
        title="Café & croissants",
        details="Adresse: 3 rue des Exemples, Sometown",
    )
    parts = urlsplit(url)
    qs = parse_qs(parts.query)
    assert qs["text"] == ["Café & croissants"]
    assert "Sometown" in qs["details"][0]


def test_is_noise_url_flags_listserv_footer_and_signature() -> None:
    assert url_extract.is_noise_url("https://groups.google.com/d/msgid/x/abc%40mail")
    assert url_extract.is_noise_url("https://www.avast.com/sig-email?utm_campaign=sig-email")
    assert url_extract.is_noise_url("https://gaggle.email/g/list/messages/x/reply")
    assert url_extract.is_noise_url("https://list.example.com/unsubscribe")
    # Bare homepage (scheme + host, no path/query) is footer-like noise.
    assert url_extract.is_noise_url("https://www.example.org/")
    # Real content links are kept.
    assert not url_extract.is_noise_url("https://docs.google.com/document/d/1abc/edit")
    assert not url_extract.is_noise_url("https://meet.google.com/sdc-ipze-nzf")


def test_filter_display_urls_drops_noise_and_caps() -> None:
    urls = [
        "https://docs.google.com/document/d/1abc/edit",
        "https://groups.google.com/d/msgid/x/abc",
        "https://www.example.org/",
        "https://meet.google.com/sdc-ipze-nzf",
    ]
    shown, hidden = url_extract.filter_display_urls(urls)
    assert shown == [
        "https://docs.google.com/document/d/1abc/edit",
        "https://meet.google.com/sdc-ipze-nzf",
    ]
    assert hidden == 0
    # Cap trims surplus non-noise links and reports the count.
    many = [f"https://docs.google.com/d/{i}/edit" for i in range(12)]
    shown, hidden = url_extract.filter_display_urls(many, limit=8)
    assert len(shown) == 8
    assert hidden == 4
