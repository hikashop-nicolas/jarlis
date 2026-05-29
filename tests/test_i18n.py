"""Tests for the i18n loader."""

from __future__ import annotations

from jarlis.i18n import (
    DEFAULT_LANG,
    available_languages,
    has_key,
    t,
)


def test_available_languages_includes_en_fr_ja() -> None:
    langs = available_languages()
    assert "en" in langs
    assert "fr" in langs
    assert "ja" in langs


def test_english_lookup_basic() -> None:
    assert t("recap.no_activity", "en") == "No activity in this period."


def test_french_translation_present() -> None:
    s = t("recap.no_activity", "fr")
    assert "Aucune activité" in s


def test_japanese_translation_present() -> None:
    s = t("recap.no_activity", "ja")
    assert "活動はありません" in s


def test_format_interpolation() -> None:
    out = t("notify.test.subject", "en", org="ACME")
    assert out == "[ACME] JARLIS test"


def test_missing_key_returns_key_visibly() -> None:
    out = t("does.not.exist", "en")
    assert out == "does.not.exist"


def test_falls_back_to_english_when_lang_missing() -> None:
    # cleanup.recap.section.people_archived only exists in en + fr; missing in ja
    out = t("cleanup.recap.section.people_archived", "ja", days=90, count=3)
    # ja.toml does not define this key; loader should fall back to English text.
    assert "Contacts archived" in out


def test_has_key_strict_lookup() -> None:
    assert has_key("recap.no_activity", "en")
    assert has_key("recap.no_activity", "ja")
    assert not has_key("does.not.exist", "en")


def test_default_lang_is_en() -> None:
    assert DEFAULT_LANG == "en"


def test_format_error_returns_unformatted_text_not_crash() -> None:
    # Missing kwarg shouldn't raise, just log + return raw template.
    out = t("notify.test.subject", "en")  # missing {org}
    assert "{org}" in out


# ---------- signature closings (cross-language data) ---------------------


def test_all_signature_closings_aggregates_languages() -> None:
    from jarlis.i18n import all_signature_closings
    closings = all_signature_closings()
    # English entries
    assert "best" in closings
    assert "kind regards" in closings
    # French entries
    assert "cordialement" in closings
    assert "bien cordialement" in closings
    # German entries (from de.toml stub)
    assert "mit freundlichen grüßen" in closings
    # Italian
    assert "cordiali saluti" in closings


def test_all_signature_closings_lower_cased() -> None:
    from jarlis.i18n import all_signature_closings
    closings = all_signature_closings()
    assert all(c == c.lower() for c in closings)
