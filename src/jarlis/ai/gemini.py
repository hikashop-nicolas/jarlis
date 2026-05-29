"""Google Gemini CLI backend.

Per Google's CLI reference and automation tutorial, headless invocation
uses two complementary inputs:

  - ``-p <text>`` carries the *instruction* (also forces non-interactive
    mode). Documented as required for scripted use.
  - stdin carries the *data* the instruction operates on. The
    ``-p`` text is "appended to stdin input if provided" per the
    cli-reference, so the model sees ``<stdin><prompt>`` concatenated.

JARLIS-internal prompts are self-contained (instruction + data woven
together), so we send the full prompt via ``-p`` and leave stdin empty.
That matches the simplest documented form (``gemini -p "..."``).

A previous version of this file piped the full prompt via stdin with no
``-p`` flag. That is undocumented: gemini in non-TTY mode appears to
treat stdin as the prompt, but the cli-reference does not guarantee it.
The current code switches to the documented contract.

ARG_MAX exposure: on macOS (1 MB) and modern Linux (≥128 KB) this is
safe for JARLIS's ~50 KB prompts. ``ps`` visibility on shared machines
is the remaining trade-off; users worried about that should pick a
different backend.

Auth uses the Gemini CLI's saved Google account or
``GEMINI_API_KEY`` if exported.
"""

from __future__ import annotations

from . import base


class GeminiBackend:
    name = "gemini"

    def __init__(
        self,
        *,
        timeout: int = 300,
        max_retries: int = 3,
        cwd: str | None = None,
        model: str | None = None,
    ) -> None:
        self.timeout = timeout
        self.max_retries = max_retries
        self.cwd = cwd
        self.model = (model or "").strip() or None
        self._checked = False

    def _ensure_cli(self) -> None:
        if not self._checked:
            base.require_cli("gemini")
            self._checked = True

    def _build_argv(self, prompt: str, *, json_output: bool) -> list[str]:
        argv = ["gemini"]
        if self.model:
            argv += ["-m", self.model]
        if json_output:
            argv += ["--output-format", "json"]
        # -p both carries the prompt and forces non-interactive mode.
        argv += ["-p", prompt]
        return argv

    def call_text(self, prompt: str) -> str:
        self._ensure_cli()
        out = base.run_cli(
            self._build_argv(prompt, json_output=False),
            stdin=None,
            timeout=self.timeout,
            max_retries=self.max_retries,
            cwd=self.cwd,
        )
        return out.strip()

    def call_json(self, prompt: str) -> dict:
        self._ensure_cli()
        out = base.run_cli(
            self._build_argv(prompt, json_output=True),
            stdin=None,
            timeout=self.timeout,
            max_retries=self.max_retries,
            cwd=self.cwd,
        )
        return base.extract_json(out)
