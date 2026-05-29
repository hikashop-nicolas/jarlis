"""Tests for jarlis.uninstall: exercise the report and dispatch logic without
actually touching the user's scheduler or keyring."""

from __future__ import annotations

import io
import sys
import tempfile
from pathlib import Path

from jarlis import uninstall as un


def _make_layout(tmp: Path, *names: str) -> Path:
    """Create a fake project root with some of the listed files/dirs."""
    for name in names:
        target = tmp / name
        if name.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x", encoding="utf-8")
    return tmp


def test_existing_finds_dirs_and_files() -> None:
    with tempfile.TemporaryDirectory() as t:
        root = _make_layout(Path(t), "memory/", "config.toml", "fetch.log")
        item_dir = un._DataItem("memory/", "memory tree")
        item_file = un._DataItem("config.toml", "config")
        item_glob = un._DataItem("*.log", "logs", is_glob=True)
        item_missing = un._DataItem("never_present.txt", "absent")
        assert un._existing(root, item_dir)
        assert un._existing(root, item_file)
        assert un._existing(root, item_glob)
        assert un._existing(root, item_missing) == []


def test_summarize_data_lists_every_known_item() -> None:
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        summary = un._summarize_data(root)
        names = [item.relpath for item, _ in summary]
        # Every USER_DATA_ITEM must be represented.
        assert len(summary) == len(un.USER_DATA_ITEMS)
        assert "config.toml" in names
        assert "memory/" in names
        assert "*.log" in names


def test_print_data_report_includes_root_and_known_items(capsys=None) -> None:
    """Sanity check the human-readable report output."""
    with tempfile.TemporaryDirectory() as t:
        root = _make_layout(Path(t), "memory/", "config.toml")
        old_stdout = sys.stdout
        buf = io.StringIO()
        sys.stdout = buf
        try:
            un._print_data_report(root)
        finally:
            sys.stdout = old_stdout
        out = buf.getvalue()
        assert str(root) in out
        # Items are listed
        assert "memory/" in out
        assert "config.toml" in out
        assert "waiting_for_approval/" in out
        # Removal commands are shown
        assert "rm -rf" in out
        assert "rm -f" in out


def test_main_scheduler_only_short_circuits(monkeypatch_storage: dict) -> None:
    """--scheduler-only must not touch keyring or print the data report."""
    calls: dict = {}

    def fake_uninstall(cfg):
        calls["scheduler"] = True
        return ["fake-entry"]

    monkeypatch_storage["orig_sched_uninstall"] = un.scheduler.uninstall
    un.scheduler.uninstall = fake_uninstall  # type: ignore[assignment]

    old_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        rc = un.main(["--scheduler-only"])
        assert rc == 0
        assert calls.get("scheduler") is True
        out = sys.stdout.getvalue()
        # Data report header should NOT appear in scheduler-only mode.
        assert "Local data" not in out
    finally:
        sys.stdout = old_stdout
        un.scheduler.uninstall = monkeypatch_storage["orig_sched_uninstall"]  # type: ignore[assignment]


def test_main_yes_flag_skips_prompts(monkeypatch_storage: dict) -> None:
    """--yes runs the keyring removal without prompting."""
    calls: dict = {"deleted": []}

    def fake_uninstall(cfg):
        return []

    def fake_delete_password(username):
        calls["deleted"].append(username)

    def fake_load_config():
        from .config_helpers import _build_cfg
        return _build_cfg()

    monkeypatch_storage["orig_sched"] = un.scheduler.uninstall
    monkeypatch_storage["orig_del"] = un.config.delete_password
    monkeypatch_storage["orig_load"] = un._load_config_or_none
    un.scheduler.uninstall = fake_uninstall  # type: ignore[assignment]
    un.config.delete_password = fake_delete_password  # type: ignore[assignment]

    # Build a fake config with usernames so the keyring step has something to do.
    from jarlis.config import Config
    cfg = Config()
    cfg.imap.username = "alice@example.com"
    cfg.smtp.username = "alice-smtp@example.com"
    un._load_config_or_none = lambda: cfg  # type: ignore[assignment]

    old_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        rc = un.main(["--yes"])
        assert rc == 0
        # Both passwords removed without prompting.
        assert "alice@example.com" in calls["deleted"]
        assert "alice-smtp@example.com" in calls["deleted"]
    finally:
        sys.stdout = old_stdout
        un.scheduler.uninstall = monkeypatch_storage["orig_sched"]  # type: ignore[assignment]
        un.config.delete_password = monkeypatch_storage["orig_del"]  # type: ignore[assignment]
        un._load_config_or_none = monkeypatch_storage["orig_load"]  # type: ignore[assignment]


def test_help_argparse_clean() -> None:
    try:
        un.main(["--help"])
    except SystemExit as e:
        assert e.code == 0
    else:
        raise AssertionError("expected --help to SystemExit(0)")
