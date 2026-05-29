"""JARLIS configuration loader.

Loads ``config.toml`` and resolves passwords via the OS keyring with a
plaintext fallback. The schema mirrors ``config.example.toml``; only fields
present in the file override the defaults defined here.

CLI helpers (``python -m jarlis.config ...``) let users store/retrieve/clear
keyring entries without writing Python.
"""

from __future__ import annotations

import logging
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

try:
    import keyring as _keyring

    HAS_KEYRING = True
except ImportError:  # pragma: no cover: exercised only on minimal installs
    _keyring = None  # type: ignore[assignment]
    HAS_KEYRING = False

log = logging.getLogger(__name__)

KEYRING_SERVICE = "jarlis"


# ---------- dataclass schema ----------------------------------------------


@dataclass
class UserConfig:
    firstname: str = ""
    lastname: str = ""
    firstname_alt: str = ""
    lastname_alt: str = ""
    email: str = ""
    # Languages the user reads in email, native FIRST.
    # languages[0] is also JARLIS's "user-facing language" (recap subject/body,
    # alerts, setup-wizard prose). Drafts adapt to the language of the incoming
    # email instead, regardless of this setting.
    languages: list[str] = field(default_factory=lambda: ["en"])
    # Aliases / extra "this is also me" addresses (forwarders, mailing-list
    # delivery via subaddresses, etc.). Used by the recipient_filter check.
    email_aliases: list[str] = field(default_factory=list)


@dataclass
class OrganizationConfig:
    name: str = ""
    url: str = ""
    # Optional footer appended to every AI-generated draft after the
    # signature line. Leave empty (the default) when the user's mail
    # client already appends an organizational signature on send: adding
    # it in the draft just bloats the notification email and wastes
    # tokens. Use only when JARLIS is the sole sender (no client footer).
    footer: str = ""


@dataclass
class IMAPConfig:
    server: str = ""
    port: int = 993
    username: str = ""
    password: str = ""  # populated at load time from keyring or plaintext fallback


@dataclass
class SMTPConfig:
    server: str = ""
    port: int = 587
    username: str = ""  # falls back to imap.username when empty
    password: str = ""


@dataclass
class NotificationConfig:
    to: str = ""


@dataclass
class AIConfig:
    backend: str = "claude"
    # Default model used for every LLM call (classifier, translation, summary,
    # bootstrap memory extraction). Empty = the CLI's own default model.
    # For ``claude``: ``sonnet``, ``opus``, ``haiku``, or a full id like
    # ``claude-sonnet-4-6``. For ``codex``/``gemini``/``llm``: pass whatever
    # model name those CLIs accept.
    model: str = ""
    # Override used **only** when generating a draft reply — the one task
    # where output quality directly shapes what the user reads. Falls back
    # to ``model`` when empty. Typical setup: ``model = "sonnet"`` for
    # cheap classification + translation, ``model_draft = "opus"`` for the
    # draft itself. Saves ~80% on tokens vs. running everything on Opus.
    model_draft: str = ""
    # Whether to expose a Read tool to the AI so it can fetch attachment
    # text on demand. When True, JARLIS inlines a short preview and provides
    # paths; the AI uses Read to fetch the rest if needed (saves tokens).
    # When False, JARLIS inlines the full extracted text up to a larger cap
    # and the AI never gets file-read access (tighter security).
    #
    # Even with Read enabled, JARLIS:
    #   - sets cwd to the project root for the AI subprocess
    #   - tells Claude (via --settings) to only allow Read on paths under
    #     the project root (defense in depth where the backend supports it)
    #   - the system prompt forbids reading anything not listed in the
    #     attachment block
    use_read_tool: bool = True


@dataclass
class PipelineConfig:
    max_per_run: int = 10
    fetch_interval: str = "20m"
    backlog_days: int = 30
    # How to treat emails where the user is not directly addressed:
    #   "all"        : process every fetched email (no filter)
    #   "addressed"  : default, process only when user is in To: OR Cc:
    #                  (skips Bcc-only / mailing-list deliveries).
    #   "primary"    : process only when user is in To: (Cc: alone is skipped)
    #   "exclusive"  : process only when user is the ONLY recipient
    # Skipped emails are archived with reason "not_addressed" and reported
    # in the recap so you stay aware of what was filtered.
    recipient_filter: str = "addressed"
    # Shared mailbox addresses where the actual sender is identified by an
    # in-body signature convention (e.g. "<name>より ... <name>") rather
    # than the From: header. JARLIS extracts the name from the body and
    # uses that as the people-memory key, so each person gets their own
    # memory file even when they all send from the same shared address.
    shared_addresses: list[str] = field(default_factory=list)


@dataclass
class RecapConfig:
    enabled: bool = True
    frequency: str = "daily"
    n_days: int = 2
    weekday: str = "fri"
    day_of_month: int = 1
    time: str = "18:00"
    custom_cron: str = ""
    # When this many emails or more share the same thread (normalized
    # subject after stripping recursive Re:/Fwd: prefixes), collapse them
    # into a single AI-narrated paragraph in the recap instead of N
    # separate rich blocks. The narration covers the low-priority,
    # not-addressed, and muted-topic sections; flagged stays per-email
    # since each one warrants attention. Set to 0 to disable.
    narrate_thread_min_count: int = 3


@dataclass
class TranslationConfig:
    """When and what to translate / summarize for the draft notification.

    All translations / summaries go through the configured AI backend, so
    they cost AI quota. Set ``translate_original`` and ``translate_draft``
    to ``false`` to save quota at the cost of having to read non-native
    content yourself.

    Attachment translation (PDF/docx/txt extracted to your primary language)
    is not yet implemented in v1 and the flag is reserved for a future
    release.
    """

    translate_original: bool = True
    translate_draft: bool = True
    # Summarize the original when its body exceeds this many characters.
    # 0 disables summarization.
    summarize_above_chars: int = 4000
    # Reserved: planned for a future release.
    translate_attachments: bool = False


@dataclass
class DraftsConfig:
    """Where JARLIS leaves AI-drafted replies.

    The local copy under ``waiting_for_approval/<slug>.md`` is always kept
    (audit trail). ``mode`` controls what JARLIS does on top of that:

      - ``email`` (default): send the draft to ``[notification].to`` so it
        lands in your normal inbox. ``Reply-To:`` is set to the original
        sender so hitting "Reply" in your mail client opens a message
        addressed correctly. You then handle the real send from wherever.
      - ``file``: only keep the local file, no email notification.
    """

    mode: str = "email"


@dataclass
class CleanupConfig:
    enabled: bool = True
    people_archive_days: int = 90
    topic_archive_days: int = 60
    draft_pending_days: int = 14
    processed_email_keep_days: int = 365
    run_on_weekday: str = "sun"


@dataclass
class Config:
    user: UserConfig = field(default_factory=UserConfig)
    organization: OrganizationConfig = field(default_factory=OrganizationConfig)
    imap: IMAPConfig = field(default_factory=IMAPConfig)
    smtp: SMTPConfig = field(default_factory=SMTPConfig)
    notification: NotificationConfig = field(default_factory=NotificationConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    drafts: DraftsConfig = field(default_factory=DraftsConfig)
    translation: TranslationConfig = field(default_factory=TranslationConfig)
    recap: RecapConfig = field(default_factory=RecapConfig)
    cleanup: CleanupConfig = field(default_factory=CleanupConfig)

    project_root: Path = field(default_factory=Path.cwd)
    config_path: Path | None = None

    # Derived paths (populated by ``init_paths``)
    memory_dir: Path = field(default_factory=Path)
    email_dir: Path = field(default_factory=Path)
    inbox_dir: Path = field(default_factory=Path)
    processed_dir: Path = field(default_factory=Path)
    archived_dir: Path = field(default_factory=Path)
    spam_dir: Path = field(default_factory=Path)
    attachments_dir: Path = field(default_factory=Path)
    error_dir: Path = field(default_factory=Path)
    queue_dir: Path = field(default_factory=Path)
    snoozed_dir: Path = field(default_factory=Path)
    pending_attention_path: Path = field(default_factory=Path)


# ---------- keyring helpers -----------------------------------------------


def _load_password(username: str, plaintext: str | None) -> str:
    """Return password from keyring, falling back to ``plaintext`` if given."""
    if not username:
        return plaintext or ""

    if HAS_KEYRING:
        try:
            pw = _keyring.get_password(KEYRING_SERVICE, username)
            if pw:
                return pw
        except Exception as exc:
            log.warning("keyring lookup for %r failed: %s", username, exc)

    if plaintext:
        return plaintext

    raise RuntimeError(
        f"No password for {username!r} in keyring (service={KEYRING_SERVICE!r}) "
        f"and no plaintext fallback in config. Run "
        f"`python -m jarlis.config set-password {username}` to store one."
    )


def store_password(username: str, password: str) -> None:
    """Store ``password`` for ``username`` in the OS keyring."""
    if not HAS_KEYRING:
        raise RuntimeError("keyring is not installed; run: pip install keyring")
    _keyring.set_password(KEYRING_SERVICE, username, password)


def get_password(username: str) -> str | None:
    """Return password from the OS keyring or ``None`` if absent."""
    if not HAS_KEYRING:
        return None
    try:
        return _keyring.get_password(KEYRING_SERVICE, username)
    except Exception as exc:
        log.warning("keyring lookup for %r failed: %s", username, exc)
        return None


def delete_password(username: str) -> None:
    """Remove a stored password from the OS keyring (no-op if absent)."""
    if not HAS_KEYRING:
        return
    try:
        _keyring.delete_password(KEYRING_SERVICE, username)
    except Exception as exc:
        log.warning("keyring delete for %r failed: %s", username, exc)


# ---------- loader --------------------------------------------------------

_CONFIG_SEARCH_PATHS: tuple[Path, ...] = (
    Path.cwd() / "config.toml",
    Path.home() / ".config" / "jarlis" / "config.toml",
    Path.home() / ".jarlis" / "config.toml",
)


def find_config_file() -> Path | None:
    """Return the first existing config.toml in the standard search paths."""
    for candidate in _CONFIG_SEARCH_PATHS:
        if candidate.exists():
            return candidate
    return None


def load_config(path: Path | str | None = None) -> Config:
    """Load and return a fully-populated ``Config``.

    Resolves IMAP/SMTP passwords via the keyring, with plaintext fallback if
    the config file contains a ``password = ...`` entry.
    """
    if path is None:
        path = find_config_file()
        if path is None:
            raise FileNotFoundError(
                "No config.toml found. Copy config.example.toml to one of: "
                + ", ".join(str(p) for p in _CONFIG_SEARCH_PATHS)
            )
    path = Path(path)

    with path.open("rb") as f:
        data = tomllib.load(f)

    cfg = Config()
    cfg.config_path = path
    cfg.project_root = path.parent.resolve()

    if "user" in data:
        cfg.user = UserConfig(**data["user"])
    if "organization" in data:
        cfg.organization = OrganizationConfig(**data["organization"])
    if "imap" in data:
        plaintext = data["imap"].pop("password", None)
        cfg.imap = IMAPConfig(**data["imap"])
        cfg.imap.password = _load_password(cfg.imap.username, plaintext)
    if "smtp" in data:
        plaintext = data["smtp"].pop("password", None)
        cfg.smtp = SMTPConfig(**data["smtp"])
        if not cfg.smtp.username:
            cfg.smtp.username = cfg.imap.username
        if not plaintext and cfg.smtp.username == cfg.imap.username:
            cfg.smtp.password = cfg.imap.password
        else:
            cfg.smtp.password = _load_password(cfg.smtp.username, plaintext)
    if "notification" in data:
        cfg.notification = NotificationConfig(**data["notification"])
    if "ai" in data:
        cfg.ai = AIConfig(**data["ai"])
    if "pipeline" in data:
        cfg.pipeline = PipelineConfig(**data["pipeline"])
    if "drafts" in data:
        cfg.drafts = DraftsConfig(**data["drafts"])
    if "translation" in data:
        cfg.translation = TranslationConfig(**data["translation"])
    if "recap" in data:
        cfg.recap = RecapConfig(**data["recap"])
    if "cleanup" in data:
        cfg.cleanup = CleanupConfig(**data["cleanup"])

    init_paths(cfg)
    return cfg


def init_paths(cfg: Config) -> None:
    """Populate derived path attributes on ``cfg`` from ``cfg.project_root``."""
    root = cfg.project_root
    cfg.memory_dir = root / "memory"
    cfg.email_dir = root / "email"
    cfg.inbox_dir = cfg.email_dir / "inbox"
    cfg.processed_dir = cfg.email_dir / "processed"
    cfg.archived_dir = cfg.processed_dir / "archived"
    cfg.spam_dir = cfg.processed_dir / "spam"
    cfg.attachments_dir = cfg.email_dir / "attachments"
    cfg.error_dir = cfg.email_dir / "error"
    cfg.queue_dir = root / "waiting_for_approval"
    cfg.snoozed_dir = root / "snoozed"
    cfg.pending_attention_path = root / "pending_attention.md"


# ---------- CLI -----------------------------------------------------------


def _cli_main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m jarlis.config <subcommand>``."""
    import argparse
    import getpass

    parser = argparse.ArgumentParser(prog="python -m jarlis.config")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_set = sub.add_parser("set-password", help="store a password in the OS keyring")
    p_set.add_argument("username")

    p_get = sub.add_parser("get-password", help="check whether a password is stored (no value printed)")
    p_get.add_argument("username")

    p_del = sub.add_parser("delete-password", help="remove a password from the OS keyring")
    p_del.add_argument("username")

    sub.add_parser("show", help="print the loaded config (passwords redacted)")
    sub.add_parser("paths", help="show the config-file search order")

    args = parser.parse_args(argv)

    if args.cmd == "set-password":
        if not HAS_KEYRING:
            print("keyring is not installed; run: pip install keyring", file=sys.stderr)
            return 2
        pw = getpass.getpass(f"password for {args.username}: ")
        store_password(args.username, pw)
        print(f"Stored password for {args.username!r} in keyring service {KEYRING_SERVICE!r}.")
        return 0

    if args.cmd == "get-password":
        if not HAS_KEYRING:
            print("keyring is not installed", file=sys.stderr)
            return 2
        pw = get_password(args.username)
        print("present" if pw else "absent")
        return 0 if pw else 1

    if args.cmd == "delete-password":
        delete_password(args.username)
        print(f"Removed keyring entry for {args.username!r} (if it existed).")
        return 0

    if args.cmd == "paths":
        cfg = find_config_file()
        for p in _CONFIG_SEARCH_PATHS:
            marker = " <- in use" if cfg == p else ""
            print(f"{p}{marker}")
        return 0

    if args.cmd == "show":
        cfg = load_config()
        # Print without exposing passwords.
        print(f"config_path:  {cfg.config_path}")
        print(f"project_root: {cfg.project_root}")
        print(f"user:         {cfg.user}")
        print(f"organization: {cfg.organization}")
        print(f"imap:         server={cfg.imap.server} port={cfg.imap.port} "
              f"user={cfg.imap.username} password={'***' if cfg.imap.password else 'unset'}")
        print(f"smtp:         server={cfg.smtp.server} port={cfg.smtp.port} "
              f"user={cfg.smtp.username} password={'***' if cfg.smtp.password else 'unset'}")
        print(f"notification: {cfg.notification}")
        print(f"ai:           {cfg.ai}")
        print(f"pipeline:     {cfg.pipeline}")
        print(f"recap:        {cfg.recap}")
        print(f"cleanup:      {cfg.cleanup}")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(_cli_main())
