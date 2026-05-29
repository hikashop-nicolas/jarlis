"""Tests for the memory tree module."""

from __future__ import annotations

import tempfile
from pathlib import Path

from jarlis import memory
from jarlis.config import Config, init_paths


def _make_cfg(tmp: Path) -> Config:
    cfg = Config()
    cfg.project_root = tmp
    init_paths(cfg)
    return cfg


def test_name_to_slug_latin_lowercases_and_underscores() -> None:
    assert memory.name_to_slug("Alice Smith") == "alice_smith"
    assert memory.name_to_slug("Bob") == "bob"
    assert memory.name_to_slug("MIXED Case") == "mixed_case"
    assert memory.name_to_slug("") == "unknown"


def test_name_to_slug_non_ascii_uses_hash() -> None:
    """CJK / Cyrillic / Arabic names get a stable hashed slug because we
    can't transliterate without an external library."""
    s = memory.name_to_slug("タナカ")
    assert s.startswith("person_")
    assert len(s) == len("person_") + 8
    # Stable: same input → same output
    assert memory.name_to_slug("タナカ") == s
    # Different input → different output
    assert memory.name_to_slug("サトウ") != s


def test_person_to_slug_dispatches_on_at_sign() -> None:
    assert memory.person_to_slug("alice@x.com") == "alice_at_x_com"
    assert memory.person_to_slug("Alice Smith") == "alice_smith"
    assert memory.person_to_slug("タナカ").startswith("person_")


def test_save_person_works_with_name_keys() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        memory.save_person(cfg, "Alice Smith", "# Alice\n\nbody")
        memory.save_person(cfg, "タナカ", "# タナカ\n\n本文")
        assert memory.load_person(cfg, "Alice Smith") == "# Alice\n\nbody"
        assert memory.load_person(cfg, "タナカ") == "# タナカ\n\n本文"
        # Roundtrip via list_people
        assert "alice_smith" in memory.list_people(cfg)
        assert any(p.startswith("person_") for p in memory.list_people(cfg))


def test_email_to_slug_handles_common_shapes() -> None:
    assert memory.email_to_slug("Sato@acme.example") == "sato_at_acme_example"
    assert memory.email_to_slug("user+tag@gmail.com") == "user_plus_tag_at_gmail_com"
    assert memory.email_to_slug("MIXED.Case@Example.COM") == "mixed_case_at_example_com"
    assert memory.email_to_slug("") == "unknown"


def test_topic_to_slug_strips_special_chars() -> None:
    assert memory.topic_to_slug("Subventions ACME") == "subventions_acme"
    assert memory.topic_to_slug("助成金 / Grants") == "grants"
    # All-special collapses to "untitled"
    assert memory.topic_to_slug("///") == "untitled"


def test_ensure_layout_creates_all_dirs() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        memory.ensure_layout(cfg)
        assert (cfg.memory_dir / "people").is_dir()
        assert (cfg.memory_dir / "topics").is_dir()
        assert (cfg.memory_dir / "voice").is_dir()
        assert (cfg.memory_dir / "archive" / "people").is_dir()
        assert (cfg.memory_dir / "archive" / "topics").is_dir()


def test_section_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        memory.save_section(cfg, "me", "# Me\n\nI am the user.")
        assert memory.load_section(cfg, "me") == "# Me\n\nI am the user."
        assert memory.load_section(cfg, "preferences") is None


def test_person_roundtrip_and_archive() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        memory.save_person(cfg, "alice@example.com", "Alice: finance team")
        assert memory.load_person(cfg, "alice@example.com") == "Alice: finance team"
        assert "alice_at_example_com" in memory.list_people(cfg)

        memory.archive_person(cfg, "alice@example.com")
        assert memory.load_person(cfg, "alice@example.com") is None
        archived = (cfg.memory_dir / "archive" / "people" / "alice_at_example_com.md")
        assert archived.exists()

        memory.restore_person(cfg, "alice@example.com")
        assert memory.load_person(cfg, "alice@example.com") == "Alice: finance team"


def test_topic_roundtrip_and_archive() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        memory.save_topic(cfg, "Subventions ACME", "Stuff about grants.")
        assert memory.load_topic(cfg, "Subventions ACME") == "Stuff about grants."
        assert "subventions_acme" in memory.list_topics(cfg)
        memory.archive_topic(cfg, "Subventions ACME")
        assert memory.load_topic(cfg, "Subventions ACME") is None


def test_voice_exemplars() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        memory.save_voice_exemplar(cfg, "fr", 1, "Bonjour, voici mon style...")
        memory.save_voice_exemplar(cfg, "fr", 2, "Cordialement, ...")
        memory.save_voice_exemplar(cfg, "ja", 1, "タナカより…")

        fr = memory.list_voice_exemplars(cfg, "fr")
        assert len(fr) == 2
        assert fr[0][0] == "exemplar_01"

        ja = memory.list_voice_exemplars(cfg, "ja")
        assert len(ja) == 1


def test_load_ignored_topics_parses_bullets_and_keywords() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        memory.save_section(
            cfg,
            "ignored_topics",
            (
                "# Topics to silence\n\n"
                "- Library cleanup announcements\n"
                "- Birthday wishlist threads (keywords: birthday, wishlist, gift)\n"
                "- All-staff lunch coordination (keywords: lunch)\n"
                "Some intro text that isn't a bullet: should be ignored.\n"
            ),
        )
        items = memory.load_ignored_topics(cfg)
        assert len(items) == 3
        assert items[0].text == "Library cleanup announcements"
        assert items[0].keywords == ["Library cleanup announcements"]
        assert items[1].text == "Birthday wishlist threads"
        assert items[1].keywords == ["birthday", "wishlist", "gift"]
        assert items[2].keywords == ["lunch"]


def test_render_for_prompt_combines_sections() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        memory.save_section(cfg, "00_organization", "# Org\n\nWe are ACME.")
        memory.save_section(cfg, "me", "# Me\n\nI'm Alice.")
        memory.save_person(cfg, "bob@acme.com", "Bob: engineering lead.")
        memory.save_topic(cfg, "deploys", "We deploy on Wednesdays.")
        memory.save_voice_exemplar(cfg, "en", 1, "Hi team, ...")

        rendered = memory.render_for_prompt(
            cfg,
            sender_email="bob@acme.com",
            languages=["en"],
            topic_slugs=["deploys"],
        )

        assert "We are ACME" in rendered
        assert "I'm Alice" in rendered
        assert "Bob: engineering lead" in rendered
        assert "We deploy on Wednesdays" in rendered
        assert "Voice exemplar (en)" in rendered
        # Sections are separated
        assert rendered.count("---") >= 4


def test_render_for_prompt_skips_missing_sender() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        memory.save_section(cfg, "me", "I'm Alice.")
        rendered = memory.render_for_prompt(cfg, sender_email="ghost@nowhere.tld")
        assert "I'm Alice" in rendered
        assert "Contact: ghost@nowhere.tld" not in rendered
