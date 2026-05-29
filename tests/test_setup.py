"""Tests for jarlis.setup: drive the config writer with synthetic answers.

The interactive Q&A path is verified manually; here we only exercise the
deterministic parts (TOML escaping, file writing, round-trip with config
loader).
"""

from __future__ import annotations

import tempfile
import tomllib
from pathlib import Path

from jarlis import config, setup


def _answers(**overrides) -> dict:
    base = {
        "firstname": "Alice",
        "lastname": "Smith",
        "firstname_alt": "アリス",
        "lastname_alt": "",
        "email": "alice@example.com",
        "languages": ["en", "fr"],
        "org_name": "ACME",
        "org_url": "https://acme.example",
        "imap_server": "imap.gmail.com",
        "imap_port": 993,
        "imap_user": "alice@example.com",
        "smtp_server": "smtp.gmail.com",
        "smtp_port": 587,
        "smtp_user": "",
        "notify_to": "alice@example.com",
        "backend": "claude",
        "model": "",
        "backlog_days": 30,
        "frequency": "daily",
        "n_days": 2,
        "weekday": "fri",
        "day_of_month": 1,
        "custom_cron": "",
        "recap_time": "18:00",
    }
    base.update(overrides)
    return base


def test_toml_quote_escapes_backslash_and_double_quote() -> None:
    assert setup._toml_quote('plain') == '"plain"'
    assert setup._toml_quote('with "quotes"') == '"with \\"quotes\\""'
    assert setup._toml_quote('back\\slash') == '"back\\\\slash"'


def test_write_config_toml_produces_parseable_output() -> None:
    with tempfile.TemporaryDirectory() as t:
        path = Path(t) / "config.toml"
        setup.write_config_toml(path, _answers())
        with path.open("rb") as f:
            data = tomllib.load(f)
        assert data["user"]["firstname"] == "Alice"
        assert data["user"]["firstname_alt"] == "アリス"
        assert data["user"]["languages"] == ["en", "fr"]
        assert data["organization"]["name"] == "ACME"
        assert data["imap"]["port"] == 993
        assert data["recap"]["frequency"] == "daily"
        assert data["recap"]["time"] == "18:00"
        # Translation defaults are present so users can find/tweak them
        assert data["translation"]["translate_original"] is True
        assert data["translation"]["translate_draft"] is True
        assert data["translation"]["summarize_above_chars"] == 4000
        assert data["translation"]["translate_attachments"] is False


def test_write_config_handles_smtp_user_blank_with_comment() -> None:
    with tempfile.TemporaryDirectory() as t:
        path = Path(t) / "config.toml"
        setup.write_config_toml(path, _answers(smtp_user=""))
        text = path.read_text(encoding="utf-8")
        assert "username defaults to imap.username" in text
        # Should still parse
        with path.open("rb") as f:
            tomllib.load(f)


def test_write_config_handles_smtp_user_set() -> None:
    with tempfile.TemporaryDirectory() as t:
        path = Path(t) / "config.toml"
        setup.write_config_toml(path, _answers(smtp_user="alice-smtp@x"))
        with path.open("rb") as f:
            data = tomllib.load(f)
        assert data["smtp"]["username"] == "alice-smtp@x"


def test_write_config_round_trip_loadable_by_config_loader() -> None:
    with tempfile.TemporaryDirectory() as t:
        path = Path(t) / "config.toml"
        # Provide blank credentials so load_config doesn't try to read keyring.
        setup.write_config_toml(path, _answers(imap_user=""))
        # Patch load_config's keyring lookup helper by clearing imap_user → no
        # password lookup is required when username is blank (see config.py).
        cfg = config.load_config(path)
        assert cfg.user.firstname == "Alice"
        assert cfg.organization.name == "ACME"
        assert cfg.recap.frequency == "daily"
        assert cfg.cleanup.processed_email_keep_days == 365


def test_write_config_preserves_special_chars_in_strings() -> None:
    with tempfile.TemporaryDirectory() as t:
        path = Path(t) / "config.toml"
        setup.write_config_toml(
            path,
            _answers(
                firstname='Quirky"Name',
                org_name='Path\\With\\Backslash',
                custom_cron='30 18 * * 1-5',
            ),
        )
        with path.open("rb") as f:
            data = tomllib.load(f)
        assert data["user"]["firstname"] == 'Quirky"Name'
        assert data["organization"]["name"] == 'Path\\With\\Backslash'
        assert data["recap"]["custom_cron"] == "30 18 * * 1-5"


def test_suggest_provider_known_domains() -> None:
    g = setup.suggest_provider("alice@gmail.com")
    assert g is not None
    assert g["imap_server"] == "imap.gmail.com"
    assert g["smtp_server"] == "smtp.gmail.com"
    assert "APP PASSWORD" in g["hint"]

    o = setup.suggest_provider("Bob@OUTLOOK.com")  # case-insensitive
    assert o is not None
    assert o["imap_server"] == "outlook.office365.com"

    fm = setup.suggest_provider("c@fastmail.com")
    assert fm is not None
    assert "fastmail" in fm["imap_server"]

    icloud = setup.suggest_provider("d@icloud.com")
    assert icloud is not None
    assert icloud["imap_server"] == "imap.mail.me.com"


def test_suggest_provider_unknown_domain_returns_none() -> None:
    assert setup.suggest_provider("user@some-niche-provider.example") is None
    assert setup.suggest_provider("") is None
    assert setup.suggest_provider("not-an-email") is None


def test_main_argparse_runs_help_clean() -> None:
    """`python -m jarlis.setup --help` should exit 0 with no exception."""
    try:
        setup.main(["--help"])
    except SystemExit as e:
        assert e.code == 0
    else:
        # Should have raised SystemExit when --help is parsed.
        raise AssertionError("expected --help to SystemExit(0)")


def test_main_help_mentions_lang_flag() -> None:
    """--help output should advertise --lang so AI agents can find it."""
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            setup.main(["--help"])
        except SystemExit:
            pass
    out = buf.getvalue()
    assert "--lang" in out
    assert "fr" in out  # example mentioned in help text


# ---------- language resolution -----------------------------------------


def test_detect_setup_lang_uses_lc_all() -> None:
    assert setup.detect_setup_lang({"LC_ALL": "fr_FR.UTF-8"}) == "fr"
    assert setup.detect_setup_lang({"LC_ALL": "ja_JP.UTF-8"}) == "ja"
    assert setup.detect_setup_lang({"LC_ALL": "en_US.UTF-8"}) == "en"


def test_detect_setup_lang_falls_through_to_lang() -> None:
    env = {"LC_ALL": "C", "LC_MESSAGES": "POSIX", "LANG": "fr_CA.UTF-8"}
    assert setup.detect_setup_lang(env) == "fr"


def test_detect_setup_lang_empty_env_returns_default() -> None:
    env = {"LC_ALL": "", "LC_MESSAGES": "", "LANG": ""}
    # locale.getlocale() may still return something; either way we get a string
    out = setup.detect_setup_lang(env)
    assert isinstance(out, str)
    assert len(out) >= 2


def test_resolve_lang_explicit_flag_wins() -> None:
    env = {"LANG": "ja_JP.UTF-8"}
    assert setup._resolve_lang("fr", env) == "fr"


def test_resolve_lang_unknown_falls_back_to_english() -> None:
    # Even with an explicit unknown lang code, we should not crash, and
    # we should fall back to English (the only language guaranteed to ship).
    assert setup._resolve_lang("klingon", {}) == "en"


def test_resolve_lang_auto_detect_when_flag_blank() -> None:
    assert setup._resolve_lang(None, {"LANG": "fr_FR.UTF-8"}) == "fr"
    assert setup._resolve_lang("", {"LANG": "ja_JP.UTF-8"}) == "ja"


def test_t_helper_returns_localized_string() -> None:
    setup.set_lang("fr")
    try:
        # ask_imap_server is in the i18n catalog
        assert setup._t("ask_imap_server") == "Serveur IMAP"
        setup.set_lang("ja")
        assert setup._t("ask_imap_server") == "IMAP サーバー"
        setup.set_lang("en")
        assert setup._t("ask_imap_server") == "IMAP server"
    finally:
        setup.set_lang("en")  # don't leak state to other tests


def test_t_helper_falls_back_to_english_for_missing_keys() -> None:
    setup.set_lang("ja")
    try:
        # All shipped keys exist in en + fr + ja, so use a synthetic
        # missing key by checking i18n's behavior directly via _t()
        out = setup._t("not_a_real_key")
        # The key itself is returned when missing in both lang and en
        assert out == "setup.not_a_real_key"
    finally:
        setup.set_lang("en")
