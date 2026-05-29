"""Common AI-backend interface and helpers.

JARLIS backends are thin subprocess wrappers around AI CLIs. They share:
  - the same Protocol (call_text / call_json)
  - retry-on-transient-failure logic
  - JSON extraction from arbitrary text output
  - read-only-by-default invocation flags (per backend's own conventions)

Backends are stateless and cheap to construct; ``get_backend(cfg)`` builds
a fresh instance per call site.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)


class AIError(RuntimeError):
    """Raised when an AI backend fails after exhausting retries."""


class AINotInstalled(AIError):
    """Raised when the requested CLI is not on PATH."""


@runtime_checkable
class AIBackend(Protocol):
    """Public Protocol any backend must satisfy."""

    name: str

    def call_text(self, prompt: str) -> str: ...
    def call_json(self, prompt: str) -> dict: ...


# ---------- helpers (used by every backend) -----------------------------


def extract_json(raw: str) -> dict:
    """Pull the first JSON object out of arbitrary CLI output.

    Handles fenced code blocks (```json ... ```) and bare ``{...}`` emits.
    Raises :class:`AIError` if no parseable object is found.
    """
    fenced = re.search(r"```(?:json)?\s*\n(.*?)\n```", raw, re.DOTALL)
    if fenced:
        candidate = fenced.group(1).strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    start = raw.find("{")
    if start == -1:
        raise AIError(f"No JSON object found in output: {raw[:200]!r}")
    depth = 0
    for i in range(start, len(raw)):
        if raw[i] == "{":
            depth += 1
        elif raw[i] == "}":
            depth -= 1
            if depth == 0:
                candidate = raw[start : i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError as e:
                    raise AIError(
                        f"JSON parse error: {e}; output: {candidate[:200]!r}"
                    ) from e
    raise AIError(f"Unbalanced braces in output: {raw[:200]!r}")


def require_cli(name: str) -> None:
    """Raise :class:`AINotInstalled` if ``name`` is not on PATH."""
    if shutil.which(name) is None:
        raise AINotInstalled(
            f"{name!r} CLI not found in PATH. "
            f"Install it first or change [ai].backend in config.toml."
        )


def _startupinfo():
    """Hide the console window on Windows; no-op elsewhere."""
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
        si.wShowWindow = 0
        return si
    return None


def run_cli(
    cmd: list[str],
    *,
    stdin: str | None = None,
    timeout: int = 300,
    max_retries: int = 3,
    cwd: str | None = None,
    non_transient_returncodes: tuple[int, ...] = (1,),
) -> str:
    """Run a subprocess with retry on transient failure. Returns stdout.

    ``non_transient_returncodes`` are exit codes treated as fatal (auth /
    invalid input): we don't retry those.
    """
    last_err: str = ""
    for attempt in range(1, max_retries + 1):
        try:
            result = subprocess.run(
                cmd,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
                startupinfo=_startupinfo(),
            )
        except subprocess.TimeoutExpired:
            last_err = "timeout"
            log.warning("CLI attempt %d/%d timed out", attempt, max_retries)
            continue
        except FileNotFoundError as e:
            raise AINotInstalled(str(e)) from e

        if result.returncode == 0:
            return result.stdout

        last_err = (result.stderr or result.stdout or "").strip() or f"rc={result.returncode}"
        log.warning(
            "CLI attempt %d/%d failed (rc=%d): %s",
            attempt, max_retries, result.returncode, last_err[:200],
        )

        if result.returncode in non_transient_returncodes:
            raise AIError(f"CLI failed: {last_err}")

    raise AIError(f"CLI failed after {max_retries} retries: {last_err}")
