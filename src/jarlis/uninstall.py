"""JARLIS uninstall helper.

Removes scheduler entries (cron / launchd / Task Scheduler), clears
keyring entries, and prints exactly which directories the user can
delete to wipe state. Does NOT delete user data automatically: the
memory tree and email archive may have value the user wants to keep.

Usage::

    python -m jarlis.uninstall                  # interactive
    python -m jarlis.uninstall --scheduler-only # just remove scheduler entries, exit
    python -m jarlis.uninstall --yes            # don't prompt, do everything

After this command runs, the user can delete the project directory by
hand (or use one of the rm/Remove-Item commands the script prints).
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from . import config, scheduler

log = logging.getLogger(__name__)


@dataclass
class _DataItem:
    relpath: str
    description: str
    is_glob: bool = False


# Items the user may want to remove. All paths relative to project_root.
USER_DATA_ITEMS: tuple[_DataItem, ...] = (
    _DataItem("config.toml",                  "your settings (organization, IMAP host, AI backend, …)"),
    _DataItem("memory/",                      "auto-extracted memory tree: org / me / contacts / topics / voice exemplars"),
    _DataItem("email/inbox/",                 "fetched but not yet processed messages"),
    _DataItem("email/processed/",             "classified messages: drafted / flagged / archived / spam"),
    _DataItem("email/attachments/",           "extracted attachments"),
    _DataItem("email/error/",                 "messages that errored during processing"),
    _DataItem("waiting_for_approval/",        "AI-drafted replies awaiting your review"),
    _DataItem("pending_attention.md",         "flagged-but-not-drafted notes"),
    _DataItem("snoozed/",                     "user-snoozed messages"),
    _DataItem("seen_email_ids.json",          "fetch dedup state"),
    _DataItem("seen_classifications.json",    "classifier cache"),
    _DataItem("last_fetch.txt",               "last successful fetch timestamp"),
    _DataItem("pipeline_health.json",         "stuck-pipeline detection state"),
    _DataItem(".last_recap_date",             "recap gating state"),
    _DataItem("*.log",                        "rotating logs",   is_glob=True),
)


def _ask_yn(prompt: str, default: bool = True) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    while True:
        try:
            answer = input(f"  {prompt}{suffix}: ").strip().lower()
        except EOFError:
            return default
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("  please answer y or n")


def _load_config_or_none():
    try:
        return config.load_config()
    except Exception as exc:
        log.info("could not load config (%s); will proceed with limited info", exc)
        return None


def _resolve_root() -> Path:
    cfg = _load_config_or_none()
    if cfg is not None:
        return cfg.project_root
    return Path.cwd()


def _existing(project_root: Path, item: _DataItem) -> list[Path]:
    """Return concrete paths matching ``item`` under ``project_root``."""
    if item.is_glob:
        return sorted(project_root.glob(item.relpath))
    p = project_root / item.relpath
    return [p] if p.exists() else []


def _summarize_data(project_root: Path) -> list[tuple[_DataItem, list[Path]]]:
    return [(item, _existing(project_root, item)) for item in USER_DATA_ITEMS]


def _print_data_report(project_root: Path) -> None:
    print()
    print("--- Local data ---")
    print()
    print("These items live under your project directory:")
    print(f"  {project_root}")
    print()
    summary = _summarize_data(project_root)
    width = max(len(item.relpath) for item, _ in summary) + 2
    for item, paths in summary:
        marker = "✓" if paths else "·"
        print(f"  {marker} {item.relpath:<{width}}  {item.description}")
    print()
    print("Commands to remove specific groups:")
    print()
    print("  # All user data (memory + email archive + drafts):")
    print(f"  rm -rf {project_root}/memory {project_root}/email \\")
    print(f"         {project_root}/waiting_for_approval {project_root}/snoozed \\")
    print(f"         {project_root}/pending_attention.md")
    print()
    print("  # JARLIS state files (fetch dedup, classifier cache, recap gating):")
    print(f"  rm -f {project_root}/seen_email_ids.json \\")
    print(f"        {project_root}/seen_classifications.json \\")
    print(f"        {project_root}/last_fetch.txt \\")
    print(f"        {project_root}/pipeline_health.json \\")
    print(f"        {project_root}/.last_recap_date \\")
    print(f"        {project_root}/*.log")
    print()
    print("  # Configuration (you'll need to re-run jarlis.setup to use JARLIS again):")
    print(f"  rm -f {project_root}/config.toml")
    print()
    print("  # Or just nuke the whole project directory:")
    print(f"  rm -rf {project_root}")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m jarlis.uninstall")
    parser.add_argument(
        "--scheduler-only", action="store_true",
        help="only remove scheduler entries, then exit",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="don't ask for confirmation; remove keyring entries automatically",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)

    print()
    print("=" * 60)
    print("JARLIS uninstall")
    print("=" * 60)

    cfg = _load_config_or_none()

    # Step 1: scheduler entries.
    print()
    print("--- Scheduler entries ---")
    try:
        # scheduler.uninstall accepts a Config but doesn't use it; pass empty if none.
        if cfg is None:
            from .config import Config as _Cfg
            cfg_for_scheduler = _Cfg()
        else:
            cfg_for_scheduler = cfg
        removed = scheduler.uninstall(cfg_for_scheduler)
        if removed:
            for r in removed:
                print(f"  ✓ removed: {r}")
        else:
            print("  (no scheduler entries found: nothing to remove)")
    except NotImplementedError as exc:
        print(f"  ⚠ unsupported platform: {exc}")
    except Exception as exc:
        print(f"  ✗ failed: {exc}")

    if args.scheduler_only:
        print()
        print("Done: scheduler entries cleared.")
        return 0

    # Step 2: keyring entries.
    print()
    print("--- Keyring entries ---")
    if cfg is None:
        print("  (no config.toml: skipping keyring cleanup)")
    else:
        usernames: list[str] = []
        if cfg.imap.username:
            usernames.append(cfg.imap.username)
        if cfg.smtp.username and cfg.smtp.username not in usernames:
            usernames.append(cfg.smtp.username)
        if not usernames:
            print("  (no IMAP/SMTP usernames in config: skipping)")
        for user in usernames:
            should_remove = args.yes or _ask_yn(f"Remove stored password for {user!r}?", True)
            if should_remove:
                config.delete_password(user)
                print(f"  ✓ removed keyring entry for {user}")
            else:
                print(f"  - kept keyring entry for {user}")

    # Step 3: report on local data + show removal commands.
    project_root = _resolve_root()
    _print_data_report(project_root)

    print()
    print("Uninstall complete.")
    print()
    print("Note: the JARLIS source code itself is left in place. Delete the project")
    print("directory above when you're ready, or `pip uninstall jarlis` to remove the")
    print("editable install from your Python environment.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
