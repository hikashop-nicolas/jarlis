"""Cross-platform scheduler installer.

Installs three recurring jobs:

    1. **pipeline** : runs ``python -m jarlis.pipeline`` every
       ``cfg.pipeline.fetch_interval`` (default ``20m``).
    2. **recap**    : runs ``python -m jarlis.recap`` daily at
       ``cfg.recap.time`` (the script self-gates based on
       ``cfg.recap.frequency``).
    3. **cleanup**  : runs ``python -m jarlis.cleanup`` weekly on
       ``cfg.cleanup.run_on_weekday``.

Backends:
    - macOS    : launchd plists in ``~/Library/LaunchAgents/``
    - Linux    : crontab entries
    - Windows  : ``schtasks`` commands

Use ``render_artifacts(cfg)`` to inspect what would be installed without
touching the system; use ``install(cfg)`` to actually apply.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)

PIPELINE_LABEL = "com.jarlis.pipeline"
RECAP_LABEL = "com.jarlis.recap"
CLEANUP_LABEL = "com.jarlis.cleanup"

WEEKDAY_NAMES: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass
class JobSpec:
    label: str
    description: str
    args: list[str]                          # python -m ... command pieces
    interval_seconds: int | None = None      # for "every N seconds" jobs
    daily_at: str | None = None              # "HH:MM" for once-a-day jobs
    weekday: str | None = None               # mon..sun for weekly jobs
    custom_cron: str | None = None           # full cron spec, overrides others


@dataclass
class Artifacts:
    """What would be installed; useful for ``--print`` mode."""

    platform: str
    pipeline: str
    recap: str
    cleanup: str
    instructions: list[str] = field(default_factory=list)


# ---------- public entry points ------------------------------------------


def render_artifacts(cfg: Config) -> Artifacts:
    jobs = _build_jobs(cfg)
    if sys.platform == "darwin":
        return _render_macos(cfg, jobs)
    if sys.platform.startswith("linux"):
        return _render_linux(cfg, jobs)
    if sys.platform == "win32":
        return _render_windows(cfg, jobs)
    raise NotImplementedError(f"unsupported platform: {sys.platform}")


def install(cfg: Config) -> Artifacts:
    artifacts = render_artifacts(cfg)
    if artifacts.platform == "darwin":
        _install_macos(cfg, artifacts)
    elif artifacts.platform == "linux":
        _install_linux(cfg, artifacts)
    elif artifacts.platform == "windows":
        _install_windows(cfg, artifacts)
    return artifacts


def uninstall(cfg: Config) -> list[str]:
    """Remove all JARLIS scheduler entries. Returns list of what was removed."""
    if sys.platform == "darwin":
        return _uninstall_macos()
    if sys.platform.startswith("linux"):
        return _uninstall_linux()
    if sys.platform == "win32":
        return _uninstall_windows()
    return []


# ---------- job specs -----------------------------------------------------


def _build_jobs(cfg: Config) -> dict[str, JobSpec]:
    pipeline = JobSpec(
        label=PIPELINE_LABEL,
        description="JARLIS pipeline (fetch + classify + route)",
        args=[sys.executable, "-m", "jarlis.pipeline"],
        interval_seconds=_parse_interval(cfg.pipeline.fetch_interval, default=1200),
    )
    recap = JobSpec(
        label=RECAP_LABEL,
        description="JARLIS recap (self-gates per config)",
        args=[sys.executable, "-m", "jarlis.recap"],
        daily_at=cfg.recap.time or "18:00",
        custom_cron=cfg.recap.custom_cron or None,
    )
    cleanup = JobSpec(
        label=CLEANUP_LABEL,
        description="JARLIS weekly cleanup",
        args=[sys.executable, "-m", "jarlis.cleanup"],
        weekday=cfg.cleanup.run_on_weekday or "sun",
        daily_at=cfg.recap.time or "18:00",
    )
    return {"pipeline": pipeline, "recap": recap, "cleanup": cleanup}


_INTERVAL_RE = re.compile(r"^\s*(\d+)\s*([smhd])?\s*$", re.IGNORECASE)


def _parse_interval(spec: str, *, default: int) -> int:
    """Parse ``20m`` / ``1h`` / ``300`` into seconds."""
    if not spec:
        return default
    m = _INTERVAL_RE.match(spec)
    if not m:
        return default
    n = int(m.group(1))
    unit = (m.group(2) or "s").lower()
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def _parse_hhmm(spec: str) -> tuple[int, int]:
    """Parse ``HH:MM`` → (hour, minute). Defaults to (18, 0) on bad input."""
    try:
        h, m = spec.split(":")
        return int(h), int(m)
    except (ValueError, AttributeError):
        return 18, 0


def _weekday_to_index(name: str) -> int:
    """Mon=1..Sun=7 (cron convention; Sun=0 also works)."""
    name_lc = (name or "sun").lower()[:3]
    mapping = {"mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 0}
    return mapping.get(name_lc, 0)


# ---------- macOS / launchd ----------------------------------------------


def _macos_agent_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _captured_env() -> dict[str, str]:
    """Capture the env vars launchd / cron need to find user-installed CLIs.

    launchd hands jobs a near-empty environment by default, so a python
    process spawned from a plist sees PATH=/usr/bin:/bin and can't find
    e.g. ``claude`` at ``~/.local/bin/claude``. We snapshot the user's
    current PATH (plus HOME / LANG / LC_ALL / SHELL) at install time and
    bake them into the plist so the scheduled job runs with the same
    environment the user uses interactively.
    """
    out: dict[str, str] = {}
    for key in ("PATH", "HOME", "LANG", "LC_ALL", "SHELL"):
        val = os.environ.get(key)
        if val:
            out[key] = val
    # Belt and suspenders: PATH is the critical one. If somehow it's
    # missing, fall back to a sensible macOS user PATH that includes
    # both Homebrew prefixes and the common ~/.local/bin.
    if "PATH" not in out:
        home = os.environ.get("HOME", str(Path.home()))
        out["PATH"] = ":".join([
            f"{home}/.local/bin",
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        ])
    return out


def _macos_env_xml(env: dict[str, str]) -> str:
    if not env:
        return ""
    lines = ["    <key>EnvironmentVariables</key>\n", "    <dict>\n"]
    for k, v in env.items():
        lines.append(f"        <key>{_xml_escape(k)}</key>\n")
        lines.append(f"        <string>{_xml_escape(v)}</string>\n")
    lines.append("    </dict>\n")
    return "".join(lines)


def _macos_plist(cfg: Config, job: JobSpec) -> str:
    args_xml = "".join(f"        <string>{_xml_escape(a)}</string>\n" for a in job.args)
    env_xml = _macos_env_xml(_captured_env())
    schedule_xml = ""
    if job.custom_cron:
        # launchd doesn't speak cron natively; document it instead of installing.
        schedule_xml = (
            "    <key>StartInterval</key>\n"
            "    <integer>3600</integer>\n"
        )
    elif job.interval_seconds:
        schedule_xml = (
            "    <key>StartInterval</key>\n"
            f"    <integer>{job.interval_seconds}</integer>\n"
        )
    elif job.weekday:
        weekday = _weekday_to_index(job.weekday)
        h, m = _parse_hhmm(job.daily_at or "18:00")
        schedule_xml = (
            "    <key>StartCalendarInterval</key>\n"
            "    <dict>\n"
            f"        <key>Weekday</key><integer>{weekday}</integer>\n"
            f"        <key>Hour</key><integer>{h}</integer>\n"
            f"        <key>Minute</key><integer>{m}</integer>\n"
            "    </dict>\n"
        )
    elif job.daily_at:
        h, m = _parse_hhmm(job.daily_at)
        schedule_xml = (
            "    <key>StartCalendarInterval</key>\n"
            "    <dict>\n"
            f"        <key>Hour</key><integer>{h}</integer>\n"
            f"        <key>Minute</key><integer>{m}</integer>\n"
            "    </dict>\n"
        )

    log_path = cfg.project_root / f"{job.label}.log"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTD/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        '<dict>\n'
        f'    <key>Label</key>\n    <string>{job.label}</string>\n'
        '    <key>ProgramArguments</key>\n'
        '    <array>\n'
        f'{args_xml}'
        '    </array>\n'
        f'    <key>WorkingDirectory</key>\n    <string>{_xml_escape(str(cfg.project_root))}</string>\n'
        f'{env_xml}'
        f'{schedule_xml}'
        f'    <key>StandardOutPath</key>\n    <string>{_xml_escape(str(log_path))}</string>\n'
        f'    <key>StandardErrorPath</key>\n    <string>{_xml_escape(str(log_path))}</string>\n'
        '    <key>RunAtLoad</key>\n    <false/>\n'
        '</dict>\n'
        '</plist>\n'
    )


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
    )


def _render_macos(cfg: Config, jobs: dict[str, JobSpec]) -> Artifacts:
    return Artifacts(
        platform="darwin",
        pipeline=_macos_plist(cfg, jobs["pipeline"]),
        recap=_macos_plist(cfg, jobs["recap"]),
        cleanup=_macos_plist(cfg, jobs["cleanup"]),
        instructions=[
            "After install, run:",
            f"  launchctl load -w {_macos_agent_dir() / (PIPELINE_LABEL + '.plist')}",
            f"  launchctl load -w {_macos_agent_dir() / (RECAP_LABEL + '.plist')}",
            f"  launchctl load -w {_macos_agent_dir() / (CLEANUP_LABEL + '.plist')}",
        ],
    )


def _install_macos(cfg: Config, artifacts: Artifacts) -> None:
    agent_dir = _macos_agent_dir()
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / f"{PIPELINE_LABEL}.plist").write_text(artifacts.pipeline, encoding="utf-8")
    (agent_dir / f"{RECAP_LABEL}.plist").write_text(artifacts.recap, encoding="utf-8")
    (agent_dir / f"{CLEANUP_LABEL}.plist").write_text(artifacts.cleanup, encoding="utf-8")
    log.info("wrote 3 plist files to %s", agent_dir)


def _uninstall_macos() -> list[str]:
    removed: list[str] = []
    agent_dir = _macos_agent_dir()
    for label in (PIPELINE_LABEL, RECAP_LABEL, CLEANUP_LABEL):
        path = agent_dir / f"{label}.plist"
        if path.exists():
            try:
                subprocess.run(["launchctl", "unload", "-w", str(path)], check=False)
            except FileNotFoundError:
                pass
            path.unlink()
            removed.append(str(path))
    return removed


# ---------- Linux / cron --------------------------------------------------


def _cron_line(cfg: Config, job: JobSpec) -> str:
    cmd = " ".join(job.args)
    # cron also runs with a near-empty PATH; export the user's PATH up front
    # so subprocesses (e.g. ``claude`` in ~/.local/bin) resolve correctly.
    path_prefix = ""
    user_path = os.environ.get("PATH")
    if user_path:
        path_prefix = f"PATH={user_path} "
    cwd = f"cd {cfg.project_root} && "
    log_path = cfg.project_root / f"{job.label}.log"
    suffix = f" >> {log_path} 2>&1"

    if job.custom_cron:
        spec = job.custom_cron
    elif job.interval_seconds and job.interval_seconds < 3600:
        minutes = max(1, job.interval_seconds // 60)
        spec = f"*/{minutes} * * * *"
    elif job.interval_seconds and job.interval_seconds % 3600 == 0:
        hours = job.interval_seconds // 3600
        spec = f"0 */{hours} * * *"
    elif job.weekday:
        h, m = _parse_hhmm(job.daily_at or "18:00")
        spec = f"{m} {h} * * {_weekday_to_index(job.weekday)}"
    elif job.daily_at:
        h, m = _parse_hhmm(job.daily_at)
        spec = f"{m} {h} * * *"
    else:
        spec = "*/20 * * * *"

    return f"{spec} {path_prefix}{cwd}{cmd}{suffix}  # {job.label}"


def _render_linux(cfg: Config, jobs: dict[str, JobSpec]) -> Artifacts:
    return Artifacts(
        platform="linux",
        pipeline=_cron_line(cfg, jobs["pipeline"]),
        recap=_cron_line(cfg, jobs["recap"]),
        cleanup=_cron_line(cfg, jobs["cleanup"]),
        instructions=[
            "These lines will be appended to your user crontab.",
            "Existing JARLIS lines (matching '# com.jarlis.') are removed first.",
            "After install, verify with: crontab -l",
        ],
    )


_CRON_MARKER = "# com.jarlis."


def _install_linux(cfg: Config, artifacts: Artifacts) -> None:
    if shutil.which("crontab") is None:
        raise RuntimeError("'crontab' not on PATH; install cron or pick another scheduler")

    existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    current = existing.stdout if existing.returncode == 0 else ""
    kept = [ln for ln in current.splitlines() if _CRON_MARKER not in ln]
    new_lines = [artifacts.pipeline, artifacts.recap, artifacts.cleanup]
    new_crontab = "\n".join(kept + new_lines) + "\n"

    proc = subprocess.run(["crontab", "-"], input=new_crontab, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"crontab install failed: {proc.stderr.strip()}")
    log.info("crontab updated with 3 JARLIS jobs")


def _uninstall_linux() -> list[str]:
    if shutil.which("crontab") is None:
        return []
    existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if existing.returncode != 0:
        return []
    kept = [ln for ln in existing.stdout.splitlines() if _CRON_MARKER not in ln]
    removed = [ln for ln in existing.stdout.splitlines() if _CRON_MARKER in ln]
    proc = subprocess.run(["crontab", "-"], input="\n".join(kept) + "\n", text=True, capture_output=True)
    if proc.returncode != 0:
        return []
    return removed


# ---------- Windows / schtasks --------------------------------------------


def _schtasks_argv(cfg: Config, job: JobSpec, task_name: str) -> list[str]:
    """Build the ``schtasks /create`` argv (no shell=True, safe vs path metachars)."""
    # /tr (task-run) is one string with the full command line schtasks will
    # invoke. We quote each arg containing whitespace so schtasks can re-parse
    # it on the Windows side. The host argv stays a clean list.
    tr = " ".join(f'"{a}"' if " " in a else a for a in job.args)
    argv = ["schtasks", "/create", "/tn", task_name]
    if job.interval_seconds and job.interval_seconds < 3600:
        minutes = max(1, job.interval_seconds // 60)
        argv += ["/sc", "minute", "/mo", str(minutes)]
    elif job.weekday:
        h, m = _parse_hhmm(job.daily_at or "18:00")
        argv += ["/sc", "weekly", "/d", job.weekday.upper(), "/st", f"{h:02d}:{m:02d}"]
    elif job.daily_at:
        h, m = _parse_hhmm(job.daily_at)
        argv += ["/sc", "daily", "/st", f"{h:02d}:{m:02d}"]
    else:
        argv += ["/sc", "minute", "/mo", "20"]
    argv += ["/tr", tr, "/f"]
    return argv


def _schtasks_preview(argv: list[str]) -> str:
    """Render an argv list as a copy-pasteable shell line for the print/preview path."""
    out: list[str] = []
    for token in argv:
        if any(c in token for c in ' "&|<>^%'):
            escaped = token.replace('"', '\\"')
            out.append(f'"{escaped}"')
        else:
            out.append(token)
    return " ".join(out)


def _render_windows(cfg: Config, jobs: dict[str, JobSpec]) -> Artifacts:
    pipeline_argv = _schtasks_argv(cfg, jobs["pipeline"], "JARLIS_Pipeline")
    recap_argv = _schtasks_argv(cfg, jobs["recap"], "JARLIS_Recap")
    cleanup_argv = _schtasks_argv(cfg, jobs["cleanup"], "JARLIS_Cleanup")
    return Artifacts(
        platform="windows",
        pipeline=_schtasks_preview(pipeline_argv),
        recap=_schtasks_preview(recap_argv),
        cleanup=_schtasks_preview(cleanup_argv),
        instructions=[
            "Run the three commands below in an Administrator PowerShell.",
            "Or run install_scheduler.py with admin privileges to apply automatically.",
        ],
    )


def _install_windows(cfg: Config, artifacts: Artifacts) -> None:
    jobs = _build_jobs(cfg)
    argvs = [
        _schtasks_argv(cfg, jobs["pipeline"], "JARLIS_Pipeline"),
        _schtasks_argv(cfg, jobs["recap"], "JARLIS_Recap"),
        _schtasks_argv(cfg, jobs["cleanup"], "JARLIS_Cleanup"),
    ]
    for argv in argvs:
        proc = subprocess.run(argv, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"schtasks failed for {argv!r}: {proc.stderr.strip()}")
    log.info("3 Windows scheduled tasks installed")


def _uninstall_windows() -> list[str]:
    removed: list[str] = []
    for name in ("JARLIS_Pipeline", "JARLIS_Recap", "JARLIS_Cleanup"):
        proc = subprocess.run(
            ["schtasks", "/delete", "/tn", name, "/f"],
            capture_output=True, text=True,
        )
        if proc.returncode == 0:
            removed.append(name)
    return removed


# ---------- CLI ----------------------------------------------------------


def _cli_main(argv: list[str] | None = None) -> int:
    import argparse
    import logging as _log

    from .config import load_config

    parser = argparse.ArgumentParser(prog="python -m jarlis.scheduler")
    parser.add_argument("action", choices=["install", "uninstall", "print"])
    args = parser.parse_args(argv)

    _log.basicConfig(level=_log.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)
    cfg = load_config()

    if args.action == "print":
        a = render_artifacts(cfg)
        print(f"# platform: {a.platform}")
        print()
        print("# --- pipeline ---")
        print(a.pipeline)
        print()
        print("# --- recap ---")
        print(a.recap)
        print()
        print("# --- cleanup ---")
        print(a.cleanup)
        if a.instructions:
            print()
            for line in a.instructions:
                print(f"# {line}")
        return 0

    if args.action == "uninstall":
        removed = uninstall(cfg)
        print(f"removed {len(removed)} scheduler entries")
        for r in removed:
            print(f"  - {r}")
        return 0

    a = install(cfg)
    print(f"installed JARLIS scheduler ({a.platform}).")
    for line in a.instructions:
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli_main())
