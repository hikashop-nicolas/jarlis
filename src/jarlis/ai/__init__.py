"""AI backend abstraction.

Public surface:

    from jarlis.ai import get_backend, AIBackend, AIError, AINotInstalled

    backend = get_backend(cfg)            # selects per cfg.ai.backend
    text = backend.call_text("...")
    obj  = backend.call_json("...")        # extract_json on raw output
"""

from .base import AIBackend, AIError, AINotInstalled, extract_json  # noqa: F401
from .claude import ClaudeBackend
from .codex import CodexBackend
from .gemini import GeminiBackend
from .llm_cli import LLMBackend

__all__ = [
    "AIBackend",
    "AIError",
    "AINotInstalled",
    "ClaudeBackend",
    "CodexBackend",
    "GeminiBackend",
    "LLMBackend",
    "extract_json",
    "get_backend",
]


def _resolve_model(cfg, task: str | None) -> str:
    """Pick the right model name for ``task`` from the config.

    The config has two knobs: ``[ai].model`` (default) and ``[ai].model_draft``
    (override for draft generation only). For ``task="draft"`` we prefer the
    override; for everything else (classifier, translation, summary,
    bootstrap) we use the default. Empty string means "let the CLI pick its
    own default" — which makes JARLIS work out-of-the-box without forcing
    the user to learn model names.
    """
    if task == "draft":
        return (cfg.ai.model_draft or cfg.ai.model or "").strip()
    return (cfg.ai.model or "").strip()


def get_backend(cfg, *, task: str | None = None, **overrides):
    """Return an instance of the backend named in ``cfg.ai.backend``.

    ``task`` lets the caller request a task-specific model:

      - ``task="draft"``: use ``[ai].model_draft`` if set, else ``[ai].model``
      - any other value (or ``None``): use ``[ai].model``

    The split exists because draft writing benefits from a higher-quality
    model (Opus / GPT-5 / Gemini Pro) while classification, translation,
    summary, and memory bootstrap work fine on cheaper, faster siblings
    (Sonnet / GPT-5-mini / Gemini Flash). With sensible config that runs
    Sonnet by default and Opus for drafts, JARLIS uses ~5x fewer tokens
    on Opus.

    Keyword overrides (``timeout``, ``max_retries``, ``cwd``,
    ``use_read_tool``, ``model``) are passed to the backend constructor.
    Defaults are sourced from ``cfg``.
    """
    name = (cfg.ai.backend or "").strip().lower()
    if "cwd" not in overrides:
        overrides["cwd"] = str(cfg.project_root)
    if "use_read_tool" not in overrides:
        overrides["use_read_tool"] = cfg.ai.use_read_tool
    if "model" not in overrides:
        overrides["model"] = _resolve_model(cfg, task) or None
    if "timeout" not in overrides:
        overrides["timeout"] = getattr(cfg.ai, "timeout", 300)
    # Backends without Read-tool semantics ignore the flag.
    if name == "claude":
        return ClaudeBackend(**overrides)
    if name == "codex":
        # Codex's Read access is governed by its sandbox flag; the backend
        # itself doesn't accept use_read_tool. Strip it.
        overrides.pop("use_read_tool", None)
        return CodexBackend(**overrides)
    if name == "gemini":
        overrides.pop("use_read_tool", None)
        return GeminiBackend(**overrides)
    if name == "llm":
        overrides.pop("use_read_tool", None)
        # ``llm`` requires an explicit model — there's no "CLI default".
        if not overrides.get("model"):
            raise AIError("config [ai].model is required when [ai].backend = 'llm'")
        return LLMBackend(**overrides)
    raise AIError(f"unknown AI backend: {name!r}")
