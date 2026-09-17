"""Single-instance guard and watchdog for the scheduled jobs.

The scheduler fires the pipeline on a fixed interval. When a run takes
longer than that interval (a hung AI CLI, a stalled IMAP socket), the next
run starts on top of the previous one. Nothing crashes, but two processes
then classify and notify in parallel.

Policy, in order:

  1. No live previous run: take the lock and go.
  2. A previous run is alive, and the last run finished cleanly: kill it,
     email the user, take the lock and go. The fresh run has the newer
     mail and a full interval ahead of it, so it is the better survivor.
  3. A previous run is alive and we already killed one last time: do not
     kill again. Something is wrong with the job itself, and killing on
     every tick would hide it. This run aborts immediately instead.

The kill counter resets on every clean release, so "twice in a row" means
two consecutive overlaps with no completed run in between.

The same module provides the watchdog: a timer that fires if the run
overruns ``timeout_seconds``, notifies, releases the lock and exits hard.
Without it, a CLI that never returns holds the lock forever and step 3
turns into a permanent refusal to run.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)

# Seconds to wait for a SIGTERM to land before escalating to SIGKILL.
KILL_GRACE_SECONDS = 10.0

# Exit code used when the watchdog kills the current run.
TIMEOUT_EXIT_CODE = 3

# Actions reported by :func:`acquire`.
ACQUIRED = "acquired"          # nothing was running
TOOK_OVER_STALE = "stale"      # lock file left behind by a dead process
KILLED_PREVIOUS = "killed"     # a live run was terminated so this one could start
ABORTED = "aborted"            # a live run was left alone; this run stands down


@dataclass
class LockDecision:
    """What :func:`acquire` did, and whether the caller may proceed."""

    proceed: bool
    action: str
    previous_pid: int | None = None
    previous_started_at: str | None = None
    consecutive_kills: int = 0
    details: dict = field(default_factory=dict)


def parse_seconds(spec: str | int | None, *, default: int = 0) -> int:
    """Parse ``"15m"`` / ``"1h"`` / ``"900"`` into seconds. 0 disables."""
    if spec is None or spec == "":
        return default
    if isinstance(spec, (int, float)):
        return int(spec)
    text = str(spec).strip().lower()
    if not text:
        return default
    unit = text[-1]
    factor = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(unit)
    try:
        if factor is None:
            return int(float(text))
        return int(float(text[:-1]) * factor)
    except ValueError:
        log.warning("could not parse duration %r; using %ds", spec, default)
        return default


def lock_path(cfg: Config, label: str) -> Path:
    return cfg.project_root / f".jarlis-{label}.lock"


def state_path(cfg: Config, label: str) -> Path:
    return cfg.project_root / f".jarlis-{label}.runstate.json"


# ---------- low-level helpers --------------------------------------------


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("could not write %s: %s", path, exc)


def pid_alive(pid: int) -> bool:
    """True if ``pid`` names a live process we are allowed to signal."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Alive, owned by someone else. Treat as alive: we must not assume
        # the slot is free just because we cannot signal it.
        return True
    except OSError:
        return False
    return True


def _reap(pid: int) -> bool:
    """Collect ``pid`` if it is our own child. True when it was reaped.

    A killed child stays a zombie until someone waits on it, and a zombie
    still answers ``kill(pid, 0)``. Without this, terminating a process we
    spawned would look like a failed kill.
    """
    try:
        done, _status = os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError, AttributeError):
        return False
    return done == pid


def terminate(pid: int, *, grace: float = KILL_GRACE_SECONDS) -> bool:
    """SIGTERM then SIGKILL. Returns True once the process is gone."""
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return not pid_alive(pid)

    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if _reap(pid) or not pid_alive(pid):
            return True
        time.sleep(0.2)

    with contextlib.suppress(ProcessLookupError, OSError):
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    time.sleep(0.2)
    return _reap(pid) or not pid_alive(pid)


# ---------- acquire / release --------------------------------------------


def acquire(cfg: Config, label: str = "pipeline", *, notifier=None) -> LockDecision:
    """Claim the run slot for ``label``. See the module docstring for policy.

    ``notifier`` is called as ``notifier(cfg, decision)`` when a previous run
    was killed or when this run stands down; it defaults to an email through
    :mod:`jarlis.notify`. Pass a no-op to stay silent (tests, manual runs).
    """
    lock = lock_path(cfg, label)
    state = _read_json(state_path(cfg, label))
    kills = int(state.get("consecutive_kills") or 0)
    current = _read_json(lock)
    previous_pid = int(current.get("pid") or 0)
    started_at = current.get("started_at")

    if not current or not previous_pid or not pid_alive(previous_pid):
        action = TOOK_OVER_STALE if current else ACQUIRED
        _claim(cfg, label, lock)
        if action == TOOK_OVER_STALE:
            log.info("%s: clearing stale lock from pid %s", label, previous_pid or "?")
        return LockDecision(True, action, previous_pid or None, started_at, kills)

    if kills >= 1:
        # We killed a run last time and are overlapping again: the job itself
        # is the problem. Stand down rather than kill on every tick.
        decision = LockDecision(
            False, ABORTED, previous_pid, started_at, kills,
            details={"reason": "previous run already killed once; not killing again"},
        )
        log.error(
            "%s: still overlapping after a kill (pid %s, started %s); aborting this run",
            label, previous_pid, started_at,
        )
        _notify(cfg, decision, notifier, label)
        return decision

    killed = terminate(previous_pid)
    kills += 1
    _write_json(
        state_path(cfg, label),
        {
            "consecutive_kills": kills,
            "last_kill_at": datetime.now().isoformat(timespec="seconds"),
            "last_killed_pid": previous_pid,
        },
    )
    log.warning(
        "%s: previous run (pid %s, started %s) was still going; %s it and taking over",
        label, previous_pid, started_at, "killed" if killed else "failed to kill",
    )
    _claim(cfg, label, lock)
    decision = LockDecision(
        True, KILLED_PREVIOUS, previous_pid, started_at, kills,
        details={"killed": killed},
    )
    _notify(cfg, decision, notifier, label)
    return decision


def _claim(cfg: Config, label: str, lock: Path) -> None:
    _write_json(
        lock,
        {
            "pid": os.getpid(),
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "host": socket.gethostname(),
            "label": label,
        },
    )


def release(cfg: Config, label: str = "pipeline", *, clean: bool = True) -> None:
    """Drop the lock. ``clean`` resets the consecutive-kill counter."""
    lock = lock_path(cfg, label)
    current = _read_json(lock)
    if current and int(current.get("pid") or 0) not in (0, os.getpid()):
        # Someone else owns it now (we were killed and took too long to
        # notice). Leave their lock alone.
        return
    try:
        lock.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("could not remove %s: %s", lock, exc)
    if clean:
        _write_json(state_path(cfg, label), {"consecutive_kills": 0})


def _notify(cfg: Config, decision: LockDecision, notifier, label: str) -> None:
    if notifier is None:
        from . import notify as _notify_mod

        notifier = _notify_mod.send_overlap_alert
    try:
        notifier(cfg, decision=decision, label=label)
    except Exception as exc:  # notifications must never break a run
        log.warning("overlap notification failed: %s", exc)


# ---------- watchdog ------------------------------------------------------


def _watchdog_fired(cfg: Config, label: str, seconds: int, notifier) -> None:
    log.error("%s: run exceeded %ds; killing it", label, seconds)
    try:
        if notifier is None:
            from . import notify as _notify_mod

            notifier = _notify_mod.send_timeout_alert
        notifier(cfg, label=label, seconds=seconds)
    except Exception as exc:
        log.warning("timeout notification failed: %s", exc)
    # Leave the kill counter alone: a timeout is not a clean finish, but the
    # lock must go or the next run would abort against a dead pid.
    release(cfg, label, clean=False)
    os._exit(TIMEOUT_EXIT_CODE)


@contextmanager
def guard(
    cfg: Config,
    label: str = "pipeline",
    *,
    timeout_seconds: int | None = None,
    notifier=None,
    timeout_notifier=None,
):
    """Context manager wrapping :func:`acquire` / :func:`release`.

    Yields the :class:`LockDecision`. When ``decision.proceed`` is False the
    body still runs, so callers must check it; that keeps the decision (and
    the reason for it) visible instead of swallowing the run silently.
    """
    decision = acquire(cfg, label, notifier=notifier)
    timer: threading.Timer | None = None
    if decision.proceed and timeout_seconds and timeout_seconds > 0:
        timer = threading.Timer(
            timeout_seconds, _watchdog_fired, args=(cfg, label, timeout_seconds, timeout_notifier)
        )
        timer.daemon = True
        timer.start()
    try:
        yield decision
    finally:
        if timer is not None:
            timer.cancel()
        if decision.proceed:
            release(cfg, label, clean=True)
