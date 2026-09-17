"""Tests for the cross-platform scheduler installer.

These cover the pure-render path (no system-touching installs). Each
platform's renderer is exercised by patching ``sys.platform``.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from jarlis import scheduler
from jarlis.config import Config, init_paths


def _make_cfg(tmp: Path) -> Config:
    cfg = Config()
    cfg.project_root = tmp
    init_paths(cfg)
    cfg.pipeline.fetch_interval = "20m"
    cfg.recap.time = "18:00"
    cfg.cleanup.run_on_weekday = "sun"
    return cfg


def _force_platform(monkeypatch_storage: dict, name: str) -> None:
    monkeypatch_storage["orig_platform"] = sys.platform
    sys.platform = name  # type: ignore[misc]


def _restore_platform(monkeypatch_storage: dict) -> None:
    sys.platform = monkeypatch_storage["orig_platform"]  # type: ignore[misc]


# ---------- interval parsing ---------------------------------------------


def test_parse_interval_units() -> None:
    assert scheduler._parse_interval("20m", default=0) == 1200
    assert scheduler._parse_interval("1h", default=0) == 3600
    assert scheduler._parse_interval("90s", default=0) == 90
    assert scheduler._parse_interval("", default=42) == 42
    assert scheduler._parse_interval("garbage", default=99) == 99


def test_parse_hhmm() -> None:
    assert scheduler._parse_hhmm("09:30") == (9, 30)
    assert scheduler._parse_hhmm("23:59") == (23, 59)
    assert scheduler._parse_hhmm("bogus") == (18, 0)


def test_weekday_to_index() -> None:
    assert scheduler._weekday_to_index("mon") == 1
    assert scheduler._weekday_to_index("Sun") == 0
    assert scheduler._weekday_to_index("garbage") == 0


# ---------- macOS plist rendering ---------------------------------------


def test_render_macos_plists_have_correct_shape(monkeypatch_storage: dict) -> None:
    _force_platform(monkeypatch_storage, "darwin")
    try:
        with tempfile.TemporaryDirectory() as t:
            cfg = _make_cfg(Path(t))
            artifacts = scheduler.render_artifacts(cfg)
            assert artifacts.platform == "darwin"
            assert "<key>Label</key>" in artifacts.pipeline
            assert scheduler.PIPELINE_LABEL in artifacts.pipeline
            assert "<key>StartInterval</key>" in artifacts.pipeline
            assert "<integer>1200</integer>" in artifacts.pipeline
            # Recap uses calendar interval
            assert "<key>StartCalendarInterval</key>" in artifacts.recap
            assert "<key>Hour</key><integer>18</integer>" in artifacts.recap
            # Cleanup uses Weekday + Hour
            assert "<key>Weekday</key>" in artifacts.cleanup
            assert "<key>Hour</key><integer>18</integer>" in artifacts.cleanup
    finally:
        _restore_platform(monkeypatch_storage)


def test_macos_plist_includes_path_environment_variable(monkeypatch_storage: dict) -> None:
    """launchd hands jobs an empty PATH; the plist must inject the user's PATH
    so subprocess calls (e.g. the ``claude`` CLI) resolve correctly.
    """
    import os as _os
    _force_platform(monkeypatch_storage, "darwin")
    saved_path = _os.environ.get("PATH")
    _os.environ["PATH"] = "/Users/test/.local/bin:/usr/bin:/bin"
    try:
        with tempfile.TemporaryDirectory() as t:
            cfg = _make_cfg(Path(t))
            artifacts = scheduler.render_artifacts(cfg)
            for plist in (artifacts.pipeline, artifacts.recap, artifacts.cleanup):
                assert "<key>EnvironmentVariables</key>" in plist
                assert "<key>PATH</key>" in plist
                assert "/Users/test/.local/bin:/usr/bin:/bin" in plist
    finally:
        if saved_path is None:
            _os.environ.pop("PATH", None)
        else:
            _os.environ["PATH"] = saved_path
        _restore_platform(monkeypatch_storage)


def test_linux_cron_line_prefixes_path(monkeypatch_storage: dict) -> None:
    """cron also runs jobs with a near-empty PATH; the line should export PATH up front."""
    import os as _os
    _force_platform(monkeypatch_storage, "linux")
    saved_path = _os.environ.get("PATH")
    _os.environ["PATH"] = "/Users/test/.local/bin:/usr/bin:/bin"
    try:
        with tempfile.TemporaryDirectory() as t:
            cfg = _make_cfg(Path(t))
            artifacts = scheduler.render_artifacts(cfg)
            assert "PATH=/Users/test/.local/bin:/usr/bin:/bin" in artifacts.pipeline
    finally:
        if saved_path is None:
            _os.environ.pop("PATH", None)
        else:
            _os.environ["PATH"] = saved_path
        _restore_platform(monkeypatch_storage)


def test_macos_plist_xml_escapes_paths(monkeypatch_storage: dict) -> None:
    _force_platform(monkeypatch_storage, "darwin")
    try:
        with tempfile.TemporaryDirectory() as t:
            cfg = _make_cfg(Path(t))
            cfg.project_root = Path("/tmp/proj with spaces & weird")
            init_paths(cfg)
            artifacts = scheduler.render_artifacts(cfg)
            # & should be escaped, the path string should be present.
            assert "&amp;" in artifacts.pipeline
            assert "with spaces &amp; weird" in artifacts.pipeline
    finally:
        _restore_platform(monkeypatch_storage)


# ---------- Linux cron rendering ----------------------------------------


def test_render_linux_pipeline_every_n_minutes(monkeypatch_storage: dict) -> None:
    _force_platform(monkeypatch_storage, "linux")
    try:
        with tempfile.TemporaryDirectory() as t:
            cfg = _make_cfg(Path(t))
            cfg.pipeline.fetch_interval = "20m"
            artifacts = scheduler.render_artifacts(cfg)
            assert artifacts.platform == "linux"
            assert artifacts.pipeline.startswith("*/20 * * * *")
            assert "jarlis.pipeline" in artifacts.pipeline
            assert "com.jarlis.pipeline" in artifacts.pipeline
    finally:
        _restore_platform(monkeypatch_storage)


def test_render_linux_recap_uses_daily_time(monkeypatch_storage: dict) -> None:
    _force_platform(monkeypatch_storage, "linux")
    try:
        with tempfile.TemporaryDirectory() as t:
            cfg = _make_cfg(Path(t))
            cfg.recap.time = "09:15"
            artifacts = scheduler.render_artifacts(cfg)
            # cron is M H * * * → "15 9 * * *"
            assert artifacts.recap.startswith("15 9 * * *")
    finally:
        _restore_platform(monkeypatch_storage)


def test_render_linux_cleanup_uses_weekday(monkeypatch_storage: dict) -> None:
    _force_platform(monkeypatch_storage, "linux")
    try:
        with tempfile.TemporaryDirectory() as t:
            cfg = _make_cfg(Path(t))
            cfg.cleanup.run_on_weekday = "fri"
            cfg.recap.time = "07:00"
            artifacts = scheduler.render_artifacts(cfg)
            # Friday = 5
            assert artifacts.cleanup.startswith("0 7 * * 5")
    finally:
        _restore_platform(monkeypatch_storage)


def test_render_linux_recap_custom_cron_overrides(monkeypatch_storage: dict) -> None:
    _force_platform(monkeypatch_storage, "linux")
    try:
        with tempfile.TemporaryDirectory() as t:
            cfg = _make_cfg(Path(t))
            cfg.recap.custom_cron = "30 4 * * *"
            artifacts = scheduler.render_artifacts(cfg)
            assert artifacts.recap.startswith("30 4 * * *")
    finally:
        _restore_platform(monkeypatch_storage)


# ---------- Windows schtasks rendering ----------------------------------


def test_render_windows_pipeline_uses_minute_schedule(monkeypatch_storage: dict) -> None:
    _force_platform(monkeypatch_storage, "win32")
    try:
        with tempfile.TemporaryDirectory() as t:
            cfg = _make_cfg(Path(t))
            cfg.pipeline.fetch_interval = "30m"
            artifacts = scheduler.render_artifacts(cfg)
            assert artifacts.platform == "windows"
            assert "schtasks" in artifacts.pipeline
            assert "/sc minute" in artifacts.pipeline
            assert "/mo 30" in artifacts.pipeline
            assert "JARLIS_Pipeline" in artifacts.pipeline
    finally:
        _restore_platform(monkeypatch_storage)


def test_render_windows_recap_daily(monkeypatch_storage: dict) -> None:
    _force_platform(monkeypatch_storage, "win32")
    try:
        with tempfile.TemporaryDirectory() as t:
            cfg = _make_cfg(Path(t))
            cfg.recap.time = "20:30"
            artifacts = scheduler.render_artifacts(cfg)
            assert "/sc daily" in artifacts.recap
            assert "/st 20:30" in artifacts.recap
    finally:
        _restore_platform(monkeypatch_storage)


def test_render_windows_cleanup_weekly(monkeypatch_storage: dict) -> None:
    _force_platform(monkeypatch_storage, "win32")
    try:
        with tempfile.TemporaryDirectory() as t:
            cfg = _make_cfg(Path(t))
            cfg.cleanup.run_on_weekday = "wed"
            artifacts = scheduler.render_artifacts(cfg)
            assert "/sc weekly" in artifacts.cleanup
            assert "/d WED" in artifacts.cleanup
    finally:
        _restore_platform(monkeypatch_storage)


def test_render_unsupported_platform_raises(monkeypatch_storage: dict) -> None:
    _force_platform(monkeypatch_storage, "haiku")
    try:
        with tempfile.TemporaryDirectory() as t:
            cfg = _make_cfg(Path(t))
            try:
                scheduler.render_artifacts(cfg)
            except NotImplementedError:
                return
            raise AssertionError("expected NotImplementedError")
    finally:
        _restore_platform(monkeypatch_storage)


# ---------- monthly maintenance job --------------------------------------


def test_render_macos_maintenance_uses_a_day_of_month(monkeypatch_storage: dict) -> None:
    _force_platform(monkeypatch_storage, "darwin")
    try:
        with tempfile.TemporaryDirectory() as t:
            cfg = _make_cfg(Path(t))
            cfg.maintenance.day_of_month = 3
            cfg.maintenance.time = "03:15"
            artifacts = scheduler.render_artifacts(cfg)
            assert scheduler.MAINTENANCE_LABEL in artifacts.maintenance
            assert "<key>StartCalendarInterval</key>" in artifacts.maintenance
            assert "<key>Day</key><integer>3</integer>" in artifacts.maintenance
            assert "<key>Hour</key><integer>3</integer>" in artifacts.maintenance
            assert "<key>Minute</key><integer>15</integer>" in artifacts.maintenance
            assert "jarlis.maintenance" in artifacts.maintenance
    finally:
        _restore_platform(monkeypatch_storage)


def test_render_linux_maintenance_is_monthly(monkeypatch_storage: dict) -> None:
    _force_platform(monkeypatch_storage, "linux")
    try:
        with tempfile.TemporaryDirectory() as t:
            cfg = _make_cfg(Path(t))
            cfg.maintenance.day_of_month = 1
            cfg.maintenance.time = "03:00"
            artifacts = scheduler.render_artifacts(cfg)
            assert artifacts.maintenance.startswith("0 3 1 * *")
            assert "com.jarlis.maintenance" in artifacts.maintenance
    finally:
        _restore_platform(monkeypatch_storage)


def test_render_windows_maintenance_is_monthly(monkeypatch_storage: dict) -> None:
    _force_platform(monkeypatch_storage, "win32")
    try:
        with tempfile.TemporaryDirectory() as t:
            cfg = _make_cfg(Path(t))
            cfg.maintenance.day_of_month = 2
            cfg.maintenance.time = "04:05"
            artifacts = scheduler.render_artifacts(cfg)
            assert "/sc monthly" in artifacts.maintenance
            assert "/d 2" in artifacts.maintenance
            assert "/st 04:05" in artifacts.maintenance
            assert "JARLIS_Maintenance" in artifacts.maintenance
    finally:
        _restore_platform(monkeypatch_storage)


def test_maintenance_day_is_clamped_into_a_month_every_month_has(monkeypatch_storage: dict) -> None:
    _force_platform(monkeypatch_storage, "linux")
    try:
        with tempfile.TemporaryDirectory() as t:
            cfg = _make_cfg(Path(t))
            cfg.maintenance.day_of_month = 31
            artifacts = scheduler.render_artifacts(cfg)
            assert artifacts.maintenance.startswith("0 3 28 * *")
    finally:
        _restore_platform(monkeypatch_storage)
