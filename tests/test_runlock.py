"""Tests for runlock.py: overlap policy, stale locks, watchdog plumbing."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from jarlis import runlock
from jarlis.config import Config, init_paths


def _make_cfg(tmp: Path) -> Config:
    cfg = Config()
    cfg.project_root = tmp
    init_paths(cfg)
    return cfg


def _spawn_sleeper() -> subprocess.Popen:
    """A real child process to stand in for a previous run."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _write_lock(cfg: Config, label: str, pid: int) -> None:
    runlock.lock_path(cfg, label).write_text(
        json.dumps({"pid": pid, "started_at": "2026-09-17T09:00:00", "label": label}),
        encoding="utf-8",
    )


# ---------- duration parsing ---------------------------------------------


def test_parse_seconds_units() -> None:
    assert runlock.parse_seconds("15m") == 900
    assert runlock.parse_seconds("1h") == 3600
    assert runlock.parse_seconds("30s") == 30
    assert runlock.parse_seconds("900") == 900
    assert runlock.parse_seconds("") == 0
    assert runlock.parse_seconds(None, default=42) == 42
    assert runlock.parse_seconds("nonsense", default=7) == 7


# ---------- acquire ------------------------------------------------------


def test_acquire_on_free_slot() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        calls: list = []
        decision = runlock.acquire(cfg, "pipeline", notifier=lambda *a, **k: calls.append(k))

        assert decision.proceed is True
        assert decision.action == runlock.ACQUIRED
        assert calls == []
        written = json.loads(runlock.lock_path(cfg, "pipeline").read_text())
        assert written["pid"] > 0


def test_stale_lock_is_taken_over_without_notifying() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        _write_lock(cfg, "pipeline", _dead_pid())
        calls: list = []

        decision = runlock.acquire(cfg, "pipeline", notifier=lambda *a, **k: calls.append(k))

        assert decision.proceed is True
        assert decision.action == runlock.TOOK_OVER_STALE
        assert calls == []


def test_live_previous_run_is_killed_and_reported() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        proc = _spawn_sleeper()
        try:
            _write_lock(cfg, "pipeline", proc.pid)
            calls: list = []

            decision = runlock.acquire(cfg, "pipeline", notifier=lambda *a, **k: calls.append(k))

            assert decision.proceed is True
            assert decision.action == runlock.KILLED_PREVIOUS
            assert decision.previous_pid == proc.pid
            assert len(calls) == 1
            assert decision.details["killed"] is True
            assert proc.poll() is not None or proc.wait(timeout=5) is not None
            state = json.loads(runlock.state_path(cfg, "pipeline").read_text())
            assert state["consecutive_kills"] == 1
        finally:
            proc.kill()
            proc.wait()


def test_second_consecutive_overlap_stands_down_instead_of_killing() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        proc = _spawn_sleeper()
        try:
            _write_lock(cfg, "pipeline", proc.pid)
            runlock.state_path(cfg, "pipeline").write_text(
                json.dumps({"consecutive_kills": 1}), encoding="utf-8"
            )
            calls: list = []

            decision = runlock.acquire(cfg, "pipeline", notifier=lambda *a, **k: calls.append(k))

            assert decision.proceed is False
            assert decision.action == runlock.ABORTED
            assert len(calls) == 1
            # The previous run is left alone, and keeps its lock.
            assert runlock.pid_alive(proc.pid)
            assert json.loads(runlock.lock_path(cfg, "pipeline").read_text())["pid"] == proc.pid
        finally:
            proc.kill()
            proc.wait()


def test_clean_release_resets_the_kill_counter() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        runlock.state_path(cfg, "pipeline").write_text(
            json.dumps({"consecutive_kills": 1}), encoding="utf-8"
        )
        runlock.acquire(cfg, "pipeline", notifier=lambda *a, **k: None)
        runlock.release(cfg, "pipeline", clean=True)

        assert not runlock.lock_path(cfg, "pipeline").exists()
        assert json.loads(runlock.state_path(cfg, "pipeline").read_text())["consecutive_kills"] == 0


def test_release_leaves_someone_elses_lock_alone() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        _write_lock(cfg, "pipeline", 999_999)
        runlock.release(cfg, "pipeline")
        assert runlock.lock_path(cfg, "pipeline").exists()


# ---------- guard --------------------------------------------------------


def test_guard_releases_on_success_and_on_error() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        with runlock.guard(cfg, "pipeline", notifier=lambda *a, **k: None) as decision:
            assert decision.proceed is True
            assert runlock.lock_path(cfg, "pipeline").exists()
        assert not runlock.lock_path(cfg, "pipeline").exists()

        try:
            with runlock.guard(cfg, "pipeline", notifier=lambda *a, **k: None):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert not runlock.lock_path(cfg, "pipeline").exists()


def test_guard_does_not_fire_the_watchdog_on_a_fast_run() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        fired: list = []
        with runlock.guard(
            cfg,
            "pipeline",
            timeout_seconds=30,
            notifier=lambda *a, **k: None,
            timeout_notifier=lambda *a, **k: fired.append(k),
        ) as decision:
            assert decision.proceed is True
            time.sleep(0.05)
        assert fired == []


def test_guard_yields_a_non_proceed_decision_when_standing_down() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        proc = _spawn_sleeper()
        try:
            _write_lock(cfg, "pipeline", proc.pid)
            runlock.state_path(cfg, "pipeline").write_text(
                json.dumps({"consecutive_kills": 1}), encoding="utf-8"
            )
            with runlock.guard(cfg, "pipeline", notifier=lambda *a, **k: None) as decision:
                assert decision.proceed is False
            # The other run's lock must survive our context exit.
            assert json.loads(runlock.lock_path(cfg, "pipeline").read_text())["pid"] == proc.pid
        finally:
            proc.kill()
            proc.wait()
