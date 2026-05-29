"""Tests for text_cleanup: clean_body and per-domain footer learning."""

from __future__ import annotations

import tempfile
from pathlib import Path

from jarlis import text_cleanup


def test_clean_body_strips_quoted_lines() -> None:
    body = "Hi Alice,\n\nThanks for the update.\n\n> On Mon, Bob wrote:\n> Earlier message\n> Another line"
    out = text_cleanup.clean_body(body)
    # Quote-intro line cuts everything after; quoted lines are also dropped
    assert "Earlier message" not in out
    assert "Thanks for the update" in out
    assert "Hi Alice" in out


def test_clean_body_strips_quoted_lines_without_intro() -> None:
    body = "Plain content here.\n> First quoted\n> > Nested quoted\nMore plain content."
    out = text_cleanup.clean_body(body)
    assert "Plain content here" in out
    assert "More plain content" in out
    assert "First quoted" not in out
    assert "Nested quoted" not in out


def test_clean_body_cuts_at_separator_line() -> None:
    body = (
        "Real content paragraph one.\n\n"
        "Real content paragraph two.\n\n"
        "***************************************\n"
        "ASSOCIATION FOOTER LINE\n"
        "more footer\n"
    )
    out = text_cleanup.clean_body(body)
    assert "Real content" in out
    assert "ASSOCIATION FOOTER" not in out
    assert "more footer" not in out


def test_clean_body_cuts_at_dash_separator() -> None:
    body = "Top of email\n----------------\nfooter text"
    out = text_cleanup.clean_body(body)
    assert "Top of email" in out
    assert "footer text" not in out


def test_clean_body_cuts_at_french_quote_intro() -> None:
    body = "Réponse en haut.\n\nLe 7 mai 2026 à 10:00, Bob <bob@x> a écrit :\n> message d'origine"
    out = text_cleanup.clean_body(body)
    assert "Réponse en haut" in out
    assert "message d'origine" not in out


def test_clean_body_strips_mobile_trailer() -> None:
    body = "Quick answer.\n\nSent from my iPhone"
    out = text_cleanup.clean_body(body)
    assert "Quick answer" in out
    assert "iPhone" not in out


def test_clean_body_strips_known_domain_footer() -> None:
    """If a domain's footer is known, ``clean_body`` removes it from the tail."""
    footer = "***************************************\nACME PARENTS ASSOCIATION\nSOMETOWN FRANCE\n***************************************"
    body = f"Hello,\n\nReal content here.\n\n{footer}"
    out = text_cleanup.clean_body(body, domain_footer=footer)
    assert "Real content" in out
    assert "ACME PARENTS" not in out


def test_clean_body_empty_returns_empty() -> None:
    assert text_cleanup.clean_body("") == ""
    assert text_cleanup.clean_body(None) == ""  # type: ignore[arg-type]


def test_longest_common_suffix_basic() -> None:
    out = text_cleanup._longest_common_suffix(["abcXYZ", "defXYZ", "ghiXYZ"])
    assert out == "XYZ"


def test_longest_common_suffix_no_match_returns_empty() -> None:
    assert text_cleanup._longest_common_suffix(["abc", "xyz"]) == ""
    assert text_cleanup._longest_common_suffix([]) == ""
    assert text_cleanup._longest_common_suffix([""]) == ""


def test_extract_candidate_footer_basic() -> None:
    body = (
        "Hello team,\n\n"
        "Some content here.\n\n"
        "Best,\nAlice\n\n"
        "***************************************\n"
        "ACME PARENTS\n"
        "SOMETOWN FRANCE\n"
        "***************************************"
    )
    out = text_cleanup.extract_candidate_footer(body)
    assert "ACME PARENTS" in out
    assert "SOMETOWN FRANCE" in out
    assert "Some content here" not in out


def test_extract_candidate_footer_skips_quoted_separator() -> None:
    """If the only separator is inside a quoted reply, skip it (look at fresh content only)."""
    body = (
        "Fresh reply here, no separator.\n\n"
        "Le 7 mai à 10:00, Bob a écrit :\n"
        "> ***\n"
        "> Quoted footer\n"
        "> ***"
    )
    out = text_cleanup.extract_candidate_footer(body)
    assert out == ""


def test_extract_candidate_footer_returns_empty_when_no_separator() -> None:
    body = "Plain reply with a signoff.\n\nBest,\nAlice"
    assert text_cleanup.extract_candidate_footer(body) == ""


def test_autodetect_footers_finds_per_domain_common_suffix() -> None:
    # Realistic footer: multi-line address block + URLs.
    footer_acme = (
        "\n\n***************************************\n"
        "ASSOCIATION DES PARENTS D'ELEVES\n"
        "ACME PARENTS ASSOCIATION\n"
        "EXAMPLE SCHOOL\n"
        "3, RUE DES EXEMPLES\n"
        "00000 SOMETOWN CEDEX 07   FRANCE\n"
        "E-mail: info@acme.example\n"
        "https://acme.example/\n"
        "***************************************"
    )
    # Include a quoted reply chain after the footer in some emails to
    # mimic real-world replies; detection should still find the footer.
    quoted = "\n\nLe 7 mai à 10:00, Bob a écrit :\n> Earlier message"
    bodies = [
        "Hello team,\n\nFirst message body. " * 3 + footer_acme,
        "Bonjour,\n\nDifferent content. " * 3 + footer_acme + quoted,
        "Hi,\n\nA third email's body. " * 3 + footer_acme + quoted,
    ]
    pairs = [("acme.example", b) for b in bodies]
    out = text_cleanup.autodetect_footers(pairs)
    assert "acme.example" in out
    assert "ACME PARENTS" in out["acme.example"]
    assert "SOMETOWN" in out["acme.example"]


def test_autodetect_footers_skips_low_email_count() -> None:
    """Need at least min_emails (default 3) bodies per domain."""
    pairs = [
        ("acme.example", "body 1\n\nFOOTER LINE 1\nFOOTER LINE 2"),
        ("acme.example", "body 2\n\nFOOTER LINE 1\nFOOTER LINE 2"),
    ]
    out = text_cleanup.autodetect_footers(pairs)
    assert "acme.example" not in out


def test_autodetect_footers_skips_short_common_suffix() -> None:
    """A 1-line trivial common suffix shouldn't qualify as a footer."""
    pairs = [
        ("acme.example", "body 1\n\n.\n"),
        ("acme.example", "body 2\n\n.\n"),
        ("acme.example", "body 3\n\n.\n"),
    ]
    out = text_cleanup.autodetect_footers(pairs)
    assert "acme.example" not in out


def test_save_load_footers_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as t:
        path = Path(t) / "footers.json"
        footers = {"acme.example": "Footer text\nLine 2", "other.tld": "Just a line"}
        text_cleanup.save_footers(path, footers)
        loaded = text_cleanup.load_footers(path)
        assert loaded == footers


def test_load_footers_missing_returns_empty() -> None:
    with tempfile.TemporaryDirectory() as t:
        path = Path(t) / "no_such.json"
        assert text_cleanup.load_footers(path) == {}


def test_clean_body_cuts_at_japanese_gmail_quote_intro() -> None:
    """Japanese Gmail's quote-intro line ('YYYY年M月D日(曜) HH:MM <name> <email>:')
    has no 'wrote:' verb but must still be detected as a quote boundary,
    otherwise actionable lines that were previously addressed in the
    quoted thread leak into the classifier prompt and trigger false drafts.
    """
    body = (
        "【スズキより 佐藤さんへ】\n"
        "\n"
        "お疲れ様です。\n"
        "情報まで連絡いたします。\n"
        "\n"
        "スズキ\n"
        "\n"
        "\n"
        "2026年5月9日(土) 9:06 Hanako Sato <hanako.sato@example.com>:\n"
        "\n"
        "> 【佐藤より タナカさん 皆さんへ】\n"
        "> タナカさん、フランス語訳の作成をお願い致します。\n"
    )
    out = text_cleanup.clean_body(body)
    assert "スズキより 佐藤さんへ" in out
    assert "スズキ" in out
    # The line above the quoted block AND the quoted body must both be cut.
    assert "Hanako Sato" not in out
    assert "タナカさん" not in out
    assert "フランス語訳" not in out


def test_domain_of_extracts_lowercase_domain() -> None:
    assert text_cleanup.domain_of("alice@Example.COM") == "example.com"
    assert text_cleanup.domain_of("noatsign") == ""
    assert text_cleanup.domain_of("") == ""
