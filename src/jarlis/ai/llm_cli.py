"""Simon Willison's ``llm`` CLI backend.

Runs ``llm -m <model>``, reading the prompt from stdin. ``llm`` is a
multi-provider wrapper supporting OpenAI, Anthropic, Gemini, Ollama, and
many others via plugins. The ``model`` argument is required and is set
from ``[ai].model`` in ``config.toml``.

This backend is also used for embeddings (see ``llm embed``) when JARLIS
adds semantic retrieval in v1.5.
"""

from __future__ import annotations

from . import base


class LLMBackend:
    name = "llm"

    def __init__(
        self,
        *,
        model: str,
        timeout: int = 300,
        max_retries: int = 3,
        cwd: str | None = None,
    ) -> None:
        if not model:
            raise ValueError("LLMBackend requires a non-empty model name")
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.cwd = cwd
        self._checked = False

    def _ensure_cli(self) -> None:
        if not self._checked:
            base.require_cli("llm")
            self._checked = True

    def call_text(self, prompt: str) -> str:
        self._ensure_cli()
        out = base.run_cli(
            ["llm", "-m", self.model],
            stdin=prompt,
            timeout=self.timeout,
            max_retries=self.max_retries,
            cwd=self.cwd,
        )
        return out.strip()

    def call_json(self, prompt: str) -> dict:
        return base.extract_json(self.call_text(prompt))
