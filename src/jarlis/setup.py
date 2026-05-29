"""JARLIS interactive setup wizard.

**Run this in your OWN terminal**: never invoke it through an AI agent's
tool-call interface. The whole point is that your IMAP and SMTP passwords
go from your keyboard straight to your OS keychain WITHOUT passing
through any LLM's context.

Usage::

    python -m jarlis.setup                  # auto-detects language from $LANG
    python -m jarlis.setup --lang fr        # force French
    python -m jarlis.setup --lang ja        # force Japanese

Language resolution order:

    1. ``--lang <code>`` if passed
    2. ``LC_ALL`` / ``LC_MESSAGES`` / ``LANG`` env var (POSIX style)
    3. ``locale.getlocale()``
    4. ``en`` (fallback)

Only languages with a matching TOML file under ``src/jarlis/i18n/`` are
honored; unknown codes silently fall back to English.

The wizard:

  1. Asks for everything JARLIS needs (name, IMAP, SMTP, AI backend, etc.)
  2. Reads passwords with :func:`getpass.getpass` so they never echo to the screen
  3. Writes ``config.toml`` next to the current directory
  4. Stores passwords in your OS keychain via the ``keyring`` library
  5. Optionally tests IMAP + sends a test SMTP ping
"""

from __future__ import annotations

import getpass
import locale
import logging
import os
from pathlib import Path

from . import config, i18n

log = logging.getLogger(__name__)

# Module-level language code used by the prompt helpers below. ``main()``
# sets this before any prompts run; tests / direct callers can override
# via ``set_lang()``.
_LANG = i18n.DEFAULT_LANG


def set_lang(lang: str) -> None:
    """Override the wizard language at runtime (used by main + tests)."""
    global _LANG
    _LANG = lang


def detect_setup_lang(env: dict | None = None) -> str:
    """Best-effort language detection for the wizard.

    Looks at LC_ALL / LC_MESSAGES / LANG env vars first (POSIX), then the
    Python ``locale`` module, then falls back to English.
    """
    env = env if env is not None else os.environ
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        val = (env.get(var) or "").strip()
        if val and val not in ("C", "POSIX", "C.UTF-8"):
            code = val.split(".")[0].split("_")[0].lower()
            if code:
                return code
    try:
        loc = locale.getlocale()
        if loc and loc[0]:
            return loc[0].split("_")[0].lower()
    except Exception:
        pass
    return i18n.DEFAULT_LANG


def _resolve_lang(requested: str | None, env: dict | None = None) -> str:
    """Pick the wizard language and validate it has a TOML file."""
    candidate = (requested or "").strip().lower() or detect_setup_lang(env)
    available = set(i18n.available_languages())
    if candidate in available:
        return candidate
    return i18n.DEFAULT_LANG


# ---------- prompt helpers ------------------------------------------------


def _t(key: str, **kwargs) -> str:
    """Localized lookup against ``setup.<key>`` using the active wizard language."""
    return i18n.t(f"setup.{key}", _LANG, **kwargs)


def _yn_aliases(yes_or_no: str) -> set[str]:
    """Return the comma-separated YN aliases from i18n strings, lower-cased."""
    raw = _t(f"yn_{yes_or_no}_aliases")
    return {a.strip().lower() for a in raw.split(",") if a.strip()}


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"  {prompt}{suffix}: ").strip()
    return answer or default


def _ask_yn(prompt: str, default: bool = True) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    yes_aliases = _yn_aliases("yes")
    no_aliases = _yn_aliases("no")
    while True:
        answer = input(f"  {prompt}{suffix}: ").strip().lower()
        if not answer:
            return default
        if answer in yes_aliases:
            return True
        if answer in no_aliases:
            return False
        print(f"  {_t('yn_invalid')}")


def _ask_int(prompt: str, default: int) -> int:
    while True:
        raw = _ask(prompt, str(default))
        try:
            return int(raw)
        except ValueError:
            print(f"  {_t('int_invalid', raw=repr(raw))}")


def _ask_choice(prompt: str, choices: list[str], default: str) -> str:
    while True:
        ans = _ask(f"{prompt} [{'/'.join(choices)}]", default).lower()
        if ans in choices:
            return ans
        print(f"  {_t('choice_invalid', choices=', '.join(choices))}")


def _ask_password(prompt: str) -> str:
    """Read a password via getpass: characters never echo to the terminal."""
    return getpass.getpass(f"  {prompt} (hidden): ")


def _toml_quote(s: str) -> str:
    """Escape a string for safe inclusion as a TOML basic string."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ---------- provider auto-detection --------------------------------------

# Common providers keyed by domain → (imap_host, imap_port, smtp_host, smtp_port, hint)
_KNOWN_PROVIDERS: dict[str, tuple[str, int, str, int, str]] = {
    "gmail.com":      ("imap.gmail.com",          993, "smtp.gmail.com",          587, "Gmail: requires an APP PASSWORD when 2FA is on"),
    "googlemail.com": ("imap.gmail.com",          993, "smtp.gmail.com",          587, "Google Mail: requires an APP PASSWORD when 2FA is on"),
    "outlook.com":    ("outlook.office365.com",   993, "smtp.office365.com",      587, "Outlook/Microsoft: app password may be required"),
    "hotmail.com":    ("outlook.office365.com",   993, "smtp.office365.com",      587, "Outlook/Microsoft: app password may be required"),
    "live.com":       ("outlook.office365.com",   993, "smtp.office365.com",      587, "Outlook/Microsoft: app password may be required"),
    "office365.com":  ("outlook.office365.com",   993, "smtp.office365.com",      587, "Office 365: see your tenant's IMAP/SMTP guide"),
    "fastmail.com":   ("imap.fastmail.com",       993, "smtp.fastmail.com",       587, "Fastmail: requires an APP PASSWORD"),
    "fastmail.fm":    ("imap.fastmail.com",       993, "smtp.fastmail.com",       587, "Fastmail: requires an APP PASSWORD"),
    "yahoo.com":      ("imap.mail.yahoo.com",     993, "smtp.mail.yahoo.com",     465, "Yahoo: requires an APP PASSWORD; SMTP uses port 465 (SSL)"),
    "icloud.com":     ("imap.mail.me.com",        993, "smtp.mail.me.com",        587, "iCloud: requires an APP-SPECIFIC PASSWORD"),
    "me.com":         ("imap.mail.me.com",        993, "smtp.mail.me.com",        587, "iCloud: requires an APP-SPECIFIC PASSWORD"),
    "mac.com":        ("imap.mail.me.com",        993, "smtp.mail.me.com",        587, "iCloud: requires an APP-SPECIFIC PASSWORD"),
    "proton.me":      ("127.0.0.1",              1143, "127.0.0.1",              1025, "ProtonMail: install ProtonMail Bridge first; uses localhost"),
    "protonmail.com": ("127.0.0.1",              1143, "127.0.0.1",              1025, "ProtonMail: install ProtonMail Bridge first; uses localhost"),
    "tutanota.com":   ("",                          0, "",                          0, "Tutanota: no IMAP/SMTP: JARLIS won't work without a bridge"),
}


def suggest_provider(email: str) -> dict | None:
    """Return ``{imap_server, imap_port, smtp_server, smtp_port, hint}`` for a known domain."""
    if "@" not in (email or ""):
        return None
    domain = email.rsplit("@", 1)[1].strip().lower()
    entry = _KNOWN_PROVIDERS.get(domain)
    if not entry:
        return None
    imap_h, imap_p, smtp_h, smtp_p, hint = entry
    return {
        "imap_server": imap_h,
        "imap_port": imap_p,
        "smtp_server": smtp_h,
        "smtp_port": smtp_p,
        "hint": hint,
    }


# ---------- config writer ------------------------------------------------


def write_config_toml(path: Path, answers: dict) -> None:
    """Render ``answers`` into a TOML file at ``path``.

    Public so tests can drive it directly without going through stdin.
    """
    languages_block = ", ".join(_toml_quote(lang) for lang in answers["languages"])
    aliases_block = ", ".join(_toml_quote(a) for a in answers.get("email_aliases", []))
    smtp_user_line = (
        f"username = {_toml_quote(answers['smtp_user'])}"
        if answers["smtp_user"]
        else "# username defaults to imap.username when blank"
    )

    content = (
        "# JARLIS configuration: written by jarlis.setup\n"
        "# Passwords are stored in your OS keychain (keyring), not here.\n"
        "\n"
        "[user]\n"
        f"firstname     = {_toml_quote(answers['firstname'])}\n"
        f"lastname      = {_toml_quote(answers['lastname'])}\n"
        f"firstname_alt = {_toml_quote(answers['firstname_alt'])}\n"
        f"lastname_alt  = {_toml_quote(answers['lastname_alt'])}\n"
        f"email         = {_toml_quote(answers['email'])}\n"
        f"languages     = [{languages_block}]\n"
        f"email_aliases = [{aliases_block}]\n"
        "\n"
        "[organization]\n"
        f"name = {_toml_quote(answers['org_name'])}\n"
        f"url  = {_toml_quote(answers['org_url'])}\n"
        "\n"
        "[imap]\n"
        f"server   = {_toml_quote(answers['imap_server'])}\n"
        f"port     = {answers['imap_port']}\n"
        f"username = {_toml_quote(answers['imap_user'])}\n"
        "\n"
        "[smtp]\n"
        f"server = {_toml_quote(answers['smtp_server'])}\n"
        f"port   = {answers['smtp_port']}\n"
        f"{smtp_user_line}\n"
        "\n"
        "[notification]\n"
        f"to = {_toml_quote(answers['notify_to'])}\n"
        "\n"
        "[drafts]\n"
        f"mode = {_toml_quote(answers.get('drafts_mode', 'email'))}\n"
        "\n"
        "[translation]\n"
        "# When the incoming email's language differs from yours (user.languages[0]),\n"
        "# include a translation in the draft notification. Costs AI quota only when\n"
        "# languages actually differ.\n"
        "translate_original = true\n"
        "# Same for the proposed draft: when JARLIS replies in another language, also\n"
        "# show you the version in your primary language so you can verify it.\n"
        "translate_draft = true\n"
        "# If the original body exceeds this many chars, include a 3-5 sentence summary.\n"
        "# Set to 0 to disable summarization.\n"
        "summarize_above_chars = 4000\n"
        "# Reserved: attachment text translation (PDF/docx). Not yet implemented.\n"
        "translate_attachments = false\n"
        "\n"
        "[ai]\n"
        f"backend = {_toml_quote(answers['backend'])}\n"
        f"model   = {_toml_quote(answers['model'])}\n"
        "\n"
        "[pipeline]\n"
        "max_per_run    = 10\n"
        "fetch_interval = \"20m\"\n"
        f"backlog_days   = {answers['backlog_days']}\n"
        f"recipient_filter = {_toml_quote(answers.get('recipient_filter', 'addressed'))}\n"
        f"shared_addresses = [{', '.join(_toml_quote(a) for a in answers.get('shared_addresses', []))}]\n"
        "\n"
        "[recap]\n"
        "enabled       = true\n"
        f"frequency     = {_toml_quote(answers['frequency'])}\n"
        f"n_days        = {answers['n_days']}\n"
        f"weekday       = {_toml_quote(answers['weekday'])}\n"
        f"day_of_month  = {answers['day_of_month']}\n"
        f"time          = {_toml_quote(answers['recap_time'])}\n"
        f"custom_cron   = {_toml_quote(answers['custom_cron'])}\n"
        "\n"
        "[cleanup]\n"
        "enabled                   = true\n"
        "people_archive_days       = 90\n"
        "topic_archive_days        = 60\n"
        "draft_pending_days        = 14\n"
        "processed_email_keep_days = 365\n"
        "run_on_weekday            = \"sun\"\n"
    )
    path.write_text(content, encoding="utf-8")


# ---------- main ---------------------------------------------------------


def collect_answers() -> tuple[dict, str, str | None]:
    """Run the full interactive Q&A. Returns (answers, imap_password, smtp_password).

    SMTP password is None when the user opted to share IMAP credentials.
    """
    print()
    print(_t("section_about_you"))
    firstname = _ask(_t("ask_firstname"))
    lastname = _ask(_t("ask_lastname"))
    firstname_alt = _ask(_t("ask_firstname_alt"))
    lastname_alt = _ask(_t("ask_lastname_alt"))
    print()
    for line in _t("source_vs_notify_explainer").splitlines():
        print(f"  {line}")
    print()
    default_email = f"{firstname.lower()}@example.tld" if firstname else ""
    email = _ask(_t("ask_source_email"), default_email)
    print()
    for line in _t("languages_explainer").splitlines():
        print(f"  {line}")
    print()
    languages_raw = _ask(_t("ask_languages"), "en")
    languages = [lang.strip() for lang in languages_raw.split(",") if lang.strip()] or ["en"]
    aliases_raw = _ask(_t("ask_aliases"), "")
    email_aliases = [a.strip() for a in aliases_raw.split(",") if a.strip()]

    print()
    print(_t("section_organization"))
    org_name = _ask(_t("ask_org_name"))
    org_url = _ask(_t("ask_org_url"))

    suggested = suggest_provider(email)
    print()
    print(_t("section_imap"))
    if suggested:
        print(f"  {_t('imap_provider_detected', email=email)}")
        if suggested["hint"]:
            print(f"  {_t('imap_provider_note', hint=suggested['hint'])}")
    else:
        print(f"  {_t('imap_provider_unknown')}")
        print(f"  {_t('imap_provider_examples')}")
    print()
    print(f"  {_t('imap_provider_doc_hint')}")
    print()
    imap_server = _ask(_t("ask_imap_server"), suggested["imap_server"] if suggested else "")
    imap_port = _ask_int(_t("ask_imap_port"), suggested["imap_port"] if suggested else 993)
    imap_user = _ask(_t("ask_imap_username"), email)
    print()
    for line in _t("imap_app_password_hint").splitlines():
        print(f"  {line}")
    print()
    imap_pass = _ask_password(_t("ask_imap_password"))

    print()
    print(_t("section_smtp"))
    smtp_server = _ask(_t("ask_smtp_server"), suggested["smtp_server"] if suggested else "")
    smtp_port = _ask_int(_t("ask_smtp_port"), suggested["smtp_port"] if suggested else 587)
    same_creds = _ask_yn(_t("ask_smtp_same_creds"), True)
    if same_creds:
        smtp_user = ""
        smtp_pass: str | None = None
    else:
        smtp_user = _ask(_t("ask_smtp_username"), imap_user)
        smtp_pass = _ask_password(_t("ask_smtp_password"))

    print()
    print(_t("section_notifications"))
    for line in _t("notify_explainer").splitlines():
        print(f"  {line}")
    notify_to = _ask(_t("ask_notify_address"), email or imap_user)
    print()
    for line in _t("drafts_mode_explainer").splitlines():
        print(f"  {line}")
    drafts_mode = _ask_choice(_t("ask_drafts_mode"), ["email", "file"], "email")

    print()
    print(_t("section_ai"))
    print(f"  {_t('ai_options_hint')}")
    backend = _ask_choice(_t("ask_ai_backend"), ["claude", "codex", "gemini", "llm"], "claude")
    model = ""
    if backend == "llm":
        model = _ask(_t("ask_ai_model"), "gpt-4o-mini")

    print()
    print(_t("section_bootstrap"))
    backlog_days = _ask_int(_t("ask_backlog_days"), 30)

    print()
    print(_t("section_recipient_filter"))
    for line in _t("recipient_filter_explainer").splitlines():
        print(f"  {line}")
    recipient_filter = _ask_choice(
        _t("ask_recipient_filter"), ["all", "addressed", "primary", "exclusive"], "addressed",
    )

    print()
    print(_t("section_recap_frequency"))
    print(f"  {_t('recap_options')}")
    frequency = _ask_choice(
        _t("ask_frequency"), ["daily", "every_n_days", "weekly", "monthly", "custom"], "daily"
    )
    n_days = 2
    weekday = "fri"
    day_of_month = 1
    custom_cron = ""
    recap_time = _ask(_t("ask_recap_time"), "18:00")
    if frequency == "every_n_days":
        n_days = _ask_int(_t("ask_n_days"), 2)
    elif frequency == "weekly":
        weekday = _ask_choice(_t("ask_weekday"), ["mon", "tue", "wed", "thu", "fri", "sat", "sun"], "fri")
    elif frequency == "monthly":
        day_of_month = _ask_int(_t("ask_day_of_month"), 1)
    elif frequency == "custom":
        custom_cron = _ask(_t("ask_custom_cron"), "0 18 * * *")

    answers = {
        "firstname": firstname,
        "lastname": lastname,
        "firstname_alt": firstname_alt,
        "lastname_alt": lastname_alt,
        "email": email,
        "languages": languages,
        "email_aliases": email_aliases,
        "org_name": org_name,
        "org_url": org_url,
        "imap_server": imap_server,
        "imap_port": imap_port,
        "imap_user": imap_user,
        "smtp_server": smtp_server,
        "smtp_port": smtp_port,
        "smtp_user": smtp_user,
        "notify_to": notify_to,
        "drafts_mode": drafts_mode,
        "shared_addresses": shared_addresses,
        "backend": backend,
        "model": model,
        "backlog_days": backlog_days,
        "recipient_filter": recipient_filter,
        "frequency": frequency,
        "n_days": n_days,
        "weekday": weekday,
        "day_of_month": day_of_month,
        "custom_cron": custom_cron,
        "recap_time": recap_time,
    }
    return answers, imap_pass, smtp_pass


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m jarlis.setup")
    parser.add_argument(
        "--config", type=Path, default=Path.cwd() / "config.toml",
        help="path to write config.toml (default: ./config.toml)",
    )
    parser.add_argument(
        "--lang",
        help=(
            "wizard language (e.g. fr, ja). Auto-detected from $LANG / locale "
            "by default. Available languages match the TOML files under "
            "src/jarlis/i18n/. Unknown codes fall back to English."
        ),
    )
    parser.add_argument(
        "--no-test", action="store_true",
        help="skip the optional IMAP and SMTP connection tests",
    )
    args = parser.parse_args(argv)

    set_lang(_resolve_lang(args.lang))

    print()
    print("=" * 60)
    print(_t("title"))
    print("=" * 60)
    print()
    for line in _t("intro").splitlines():
        print(line)
    print()

    if not config.HAS_KEYRING:
        print(_t("no_keyring_warning"))
        print(f"  {_t('no_keyring_needed')}")
        print(f"  {_t('no_keyring_install')}")
        print()
        if not _ask_yn(_t("continue_no_keyring"), False):
            return 2

    answers, imap_pass, smtp_pass = collect_answers()

    config_path = args.config
    print()
    print(_t("section_summary"))
    print(f"  {_t('summary_config_path', path=str(config_path))}")
    print(f"  {_t('summary_imap_keyring', service=config.KEYRING_SERVICE, user=answers['imap_user'])}")
    if smtp_pass is not None:
        print(f"  {_t('summary_smtp_keyring', service=config.KEYRING_SERVICE, user=answers['smtp_user'])}")
    print()
    if not _ask_yn(_t("ask_confirm_write"), True):
        print(_t("aborted"))
        return 1

    write_config_toml(config_path, answers)
    print(f"  {_t('wrote_config', path=str(config_path))}")

    if config.HAS_KEYRING:
        config.store_password(answers["imap_user"], imap_pass)
        print(f"  {_t('stored_imap_password', user=answers['imap_user'])}")
        if smtp_pass and answers["smtp_user"]:
            config.store_password(answers["smtp_user"], smtp_pass)
            print(f"  {_t('stored_smtp_password', user=answers['smtp_user'])}")
    else:
        print(f"  {_t('skipped_keyring')}")

    if not args.no_test:
        _maybe_test_connections(config_path)

    print()
    print(_t("done_header"))
    print()
    print(_t("next_steps_header"))
    print(f"  {_t('next_step_bootstrap', days=answers['backlog_days'])}")
    print(f"     {_t('next_step_bootstrap_subtitle', days=answers['backlog_days'], backend=answers['backend'])}")
    print(f"  {_t('next_step_review')}")
    print(f"  {_t('next_step_scheduler')}")
    print()
    for line in _t("ai_session_hint").splitlines():
        print(line)
    return 0


def _maybe_test_connections(config_path: Path) -> None:
    """Optional IMAP + SMTP smoke tests. Failures are reported, not fatal."""
    print()
    if _ask_yn(_t("ask_test_imap"), True):
        try:
            from . import imap_fetch  # local import to avoid circular at module load

            cfg = config.load_config(config_path)
            mail = imap_fetch.connect_imap(cfg)
            try:
                mail.logout()
            except Exception:
                pass
            print(f"  {_t('imap_test_pass')}")
        except Exception as exc:
            print(f"  {_t('imap_test_fail', exc=exc)}")
            for line in _t("imap_test_recovery").splitlines():
                print(f"    {line}")

    if _ask_yn(_t("ask_test_smtp"), True):
        try:
            from . import notify

            cfg = config.load_config(config_path)
            ok = notify.send_test_ping(cfg)
            print(f"  {_t('smtp_test_pass') if ok else _t('smtp_test_fail_returned_false')}")
        except Exception as exc:
            print(f"  {_t('smtp_test_fail_exc', exc=exc)}")


if __name__ == "__main__":
    import sys

    sys.exit(main())
