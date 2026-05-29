"""OpenAI Codex CLI backend.

Runs ``codex exec --sandbox read-only -``. Stdout receives the final
message; stderr receives progress and is ignored. The ``read-only``
sandbox prevents the model from editing or executing.

Auth uses Codex's saved CLI credentials, or ``CODEX_API_KEY`` if exported.
"""

from __future__ import annotations

from . import base


class CodexBackend:
    name = "codex"

    def __init__(
        self,
        *,
        timeout: int = 300,
        max_retries: int = 3,
        cwd: str | None = None,
        sandbox: str = "read-only",
        model: str | None = None,
    ) -> None:
        self.timeout = timeout
        self.max_retries = max_retries
        self.cwd = cwd
        self.sandbox = sandbox
        self.model = (model or "").strip() or None
        self._checked = False

    def _ensure_cli(self) -> None:
        if not self._checked:
            base.require_cli("codex")
            self._checked = True

    def call_text(self, prompt: str) -> str:
        self._ensure_cli()
        argv = ["codex", "exec", "--sandbox", self.sandbox]
        if self.model:
            argv += ["--model", self.model]
        argv.append("-")  # read prompt from stdin
        out = base.run_cli(
            argv,
            stdin=prompt,
            timeout=self.timeout,
            max_retries=self.max_retries,
            cwd=self.cwd,
        )
        return out.strip()

    def call_json(self, prompt: str) -> dict:
        return base.extract_json(self.call_text(prompt))
