"""Tests for voice exemplar selection."""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

from jarlis import memory, voice
from jarlis.config import Config, init_paths
from jarlis.models import Email


def _make_cfg(tmp: Path) -> Config:
    cfg = Config()
    cfg.project_root = tmp
    init_paths(cfg)
    memory.ensure_layout(cfg)
    return cfg


def _email(body: str, *, to: list[str] | None = None, subject: str = "Hi", days_ago: int = 0) -> Email:
    return Email(
        message_id=f"<{hash(body) % 10**8}@x>",
        sender="me@x.com",
        subject=subject,
        body_text=body,
        to=to or ["someone@x.com"],
        date=datetime(2026, 5, 7 - days_ago, 10, 0),
    )


def test_detect_language_japanese() -> None:
    text = "タナカより、お疲れ様です。今週もよろしくお願いします。"
    assert voice.detect_language(text) == "ja"


def test_detect_language_french() -> None:
    text = "Bonjour Bob, merci pour ton message. Je te confirme cela demain. Bien à vous, Alice"
    assert voice.detect_language(text) == "fr"


def test_detect_language_english_default() -> None:
    text = "Hi team, just confirming the next deploy window. Best, Alice"
    assert voice.detect_language(text) == "en"


def test_heuristic_detects_spanish_and_german() -> None:
    # Use the heuristic directly so the test is deterministic regardless
    # of whether langdetect is installed.
    es = "Hola Bob, gracias por tu mensaje. Saludos cordialmente, Alice por favor"
    de = "Hallo Bob, danke für deine Nachricht. Mit freundlichen Grüßen, Alice bitte"
    assert voice._heuristic_detect(es) == "es"
    assert voice._heuristic_detect(de) == "de"


def test_heuristic_detects_korean() -> None:
    text = "안녕하세요 밥씨, 메시지 감사합니다. 다음 주에 답변 드리겠습니다."
    assert voice._heuristic_detect(text) == "ko"


def test_heuristic_detects_russian() -> None:
    text = "Здравствуйте Боб, спасибо за ваше сообщение. С уважением, Алиса"
    assert voice._heuristic_detect(text) == "ru"


def test_heuristic_detects_arabic() -> None:
    text = "مرحبا بوب، شكرا لرسالتك. سأعود إليك قريبا. مع التحية، أليس"
    assert voice._heuristic_detect(text) == "ar"


def test_heuristic_empty_returns_english() -> None:
    assert voice._heuristic_detect("") == "en"
    assert voice._heuristic_detect("?") == "en"


def test_select_exemplars_respects_per_lang_cap() -> None:
    body = "Hi friend, " + ("here is some content of normal length. " * 5)
    emails = [_email(body=body, subject=f"Subject {i}", to=[f"r{i}@x"]) for i in range(20)]
    out = voice.select_exemplars(emails, per_lang=3)
    assert sum(len(v) for v in out.values()) <= 3


def test_select_exemplars_skips_too_short_and_too_long() -> None:
    out = voice.select_exemplars(
        [
            _email("too short", to=["a@x"]),
            _email("x" * 10_000, to=["b@x"]),
        ],
        per_lang=5,
    )
    # Both should be filtered out.
    assert sum(len(v) for v in out.values()) == 0


def test_select_exemplars_skips_distribution_lists() -> None:
    body = "Hi all, " + ("this is body content. " * 10)
    out = voice.select_exemplars(
        # > MAX_RECIPIENTS (15) so this gets dropped as a distribution list
        [_email(body=body, to=[f"u{i}@x" for i in range(20)])],
        per_lang=5,
    )
    assert sum(len(v) for v in out.values()) == 0


def test_select_exemplars_admits_bureau_threads_within_recipient_cap() -> None:
    """Threads with up to MAX_RECIPIENTS members (a typical bureau) are kept."""
    body = "Hi team, here is the project update for the week. " * 4
    out = voice.select_exemplars(
        [_email(body=body, to=[f"member{i}@x" for i in range(12)])],
        per_lang=5,
    )
    assert sum(len(v) for v in out.values()) == 1


def test_select_exemplars_dedupes_by_prefix() -> None:
    body = ("Hi friend, here is the same opening as before. " * 3)
    out = voice.select_exemplars(
        [
            _email(body=body, to=["a@x"]),
            _email(body=body, to=["b@x"]),  # same prefix
        ],
        per_lang=5,
    )
    total = sum(len(v) for v in out.values())
    assert total == 1


def test_select_exemplars_keeps_distinct_emails_to_same_recipient() -> None:
    """We dropped the per-recipient dedup; multiple emails to the same person
    survive as long as their opening prefixes differ."""
    same_recipient = ["bob@x.com"]
    bodies = [
        "Hi Bob, here is reply A about something. " * 3,
        "Hello Bob, here is reply B about something else. " * 3,
        "Bonjour Bob, voici la réponse C concernant un autre sujet. " * 3,
    ]
    emails = [_email(body=b, to=same_recipient, subject=f"S{i}") for i, b in enumerate(bodies)]
    out = voice.select_exemplars(emails, per_lang=5)
    # All three have distinct prefixes → all kept (regardless of recipient).
    assert sum(len(v) for v in out.values()) == 3


def test_save_exemplars_writes_per_lang_files() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        en_body = "Hi team, here is the project update for this week. Best, Alice. " * 3
        fr_body = "Bonjour Bob, merci pour ton message, je confirme cela demain. Bien à vous, Alice. " * 2
        emails = [
            _email(body=en_body, subject="EN-1", to=["x@x"]),
            _email(body=fr_body, subject="FR-1", to=["y@y"]),
        ]
        out = voice.select_exemplars(emails, per_lang=5)
        paths = voice.save_exemplars(cfg, out)
        assert "en" in paths
        assert "fr" in paths
        # Files exist on disk
        assert (cfg.memory_dir / "voice" / "en" / "exemplar_01.md").exists()
        assert (cfg.memory_dir / "voice" / "fr" / "exemplar_01.md").exists()


def test_jarlis_notification_detected_by_signature_line() -> None:
    """An outgoing JARLIS notification (signed 'JARLIS') is not 'user voice'."""
    e = Email(
        message_id="<m@x>",
        sender="user@example.com",
        subject="[ACME] Test JARLIS",
        body_text="Hi Alice,\n\nThis is a JARLIS test message.\n\nJARLIS",
        to=["alice@personal.tld"],
    )
    assert voice._looks_like_jarlis_notification(e, notify_to="alice@personal.tld")


def test_jarlis_notification_detected_by_to_plus_bracket_subject() -> None:
    """Even without the JARLIS signoff, To=notification + [Org] subject is enough."""
    e = Email(
        message_id="<m@x>",
        sender="user@example.com",
        subject="[ACME] Brouillon pour: project update",
        body_text="Long French body\nwith varied content\nand no canonical signoff.",
        to=["alice@personal.tld"],
    )
    assert voice._looks_like_jarlis_notification(e, notify_to="alice@personal.tld")


def test_genuine_user_email_not_misdetected() -> None:
    """Real user emails to the notification address shouldn't be mistaken for JARLIS."""
    e = Email(
        message_id="<m@x>",
        sender="user@example.com",
        subject="lunch tomorrow?",  # plain subject, no [Org] bracket
        body_text="Hey, want to grab lunch tomorrow at noon? Let me know.",
        to=["alice@personal.tld"],
    )
    assert not voice._looks_like_jarlis_notification(e, notify_to="alice@personal.tld")


def test_select_exemplars_filters_out_jarlis_notifications() -> None:
    """End-to-end: a JARLIS test ping in the sent pool doesn't become an exemplar."""
    notif = Email(
        message_id="<n@x>",
        sender="me@x.com",
        subject="[ACME] Test JARLIS",
        body_text="Bonjour Alice,\n\nCeci est un message de test JARLIS.\n\nJARLIS",
        to=["alice@personal.tld"],
        date=datetime(2026, 5, 7, 10, 0),
    )
    real = _email(
        body=(
            "Bonjour Bob, merci pour ton message. Je te confirme que le budget "
            "sera revu lors de la prochaine réunion. Bien à vous, Alice " * 2
        ),
        subject="Re: budget",
        to=["bob@partner.tld"],
        days_ago=1,
    )
    out = voice.select_exemplars([notif, real], per_lang=5, notify_to="alice@personal.tld")
    # Only the real email survived
    total = sum(len(v) for v in out.values())
    assert total == 1


def test_render_exemplar_quotes_body() -> None:
    e = _email("line one\nline two\nline three")
    out = voice.render_exemplar(e)
    assert "> line one" in out
    assert "> line two" in out
    assert "> line three" in out
