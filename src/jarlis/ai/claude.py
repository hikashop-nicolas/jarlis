"""Claude Code CLI backend.

Runs ``claude -p`` in read-only mode. When the Read tool is enabled, we
also pass a ``--settings`` JSON string that scopes Read permissions to
paths under the project root. This is defense-in-depth: the system prompt
already tells the model not to read unrelated files, but the settings
constraint blocks the action even if the model somehow tried.
"""

from __future__ import annotations

import json

from . import base


class ClaudeBackend:
    name = "claude"

    def __init__(
        self,
        *,
        timeout: int = 300,
        max_retries: int = 3,
        cwd: str | None = None,
        use_read_tool: bool = True,
        model: str | None = None,
    ) -> None:
        self.timeout = timeout
        self.max_retries = max_retries
        self.cwd = cwd
        self.use_read_tool = use_read_tool
        self.model = (model or "").strip() or None
        self._checked = False

    def _ensure_cli(self) -> None:
        if not self._checked:
            base.require_cli("claude")
            self._checked = True

    def _build_argv(self) -> list[str]:
        argv = ["claude", "-p", "--output-format", "text"]
        if self.model:
            argv += ["--model", self.model]
        if self.use_read_tool:
            argv += ["--allowedTools", "Read"]
            # Scope Read tool permissions to the project directory. Claude
            # interprets these patterns relative to the working directory.
            scope = self.cwd or "."
            settings = {
                "permissions": {
                    "allow": [
                        f"Read({scope}/**)",
                    ],
                    "deny": [
                        # Belt-and-braces: explicitly block common sensitive
                        # paths in case the model walks out of cwd somehow.
                        "Read(/etc/**)",
                        "Read(~/.ssh/**)",
                        "Read(~/.aws/**)",
                        "Read(~/.config/jarlis/**)",
                        "Read(~/.jarlis/**)",
                    ],
                }
            }
            argv += ["--settings", json.dumps(settings)]
        else:
            # Disable all tools so the AI can't read anything. Trades tokens
            # for security: callers must inline full attachment content.
            argv += ["--disallowedTools", "Read", "Edit", "Write", "Bash", "WebFetch"]
        return argv

    def call_text(self, prompt: str) -> str:
        self._ensure_cli()
        out = base.run_cli(
            self._build_argv(),
            stdin=prompt,
            timeout=self.timeout,
            max_retries=self.max_retries,
            cwd=self.cwd,
        )
        return out.strip()

    def call_json(self, prompt: str) -> dict:
        return base.extract_json(self.call_text(prompt))
