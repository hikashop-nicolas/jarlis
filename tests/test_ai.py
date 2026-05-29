"""Tests for the AI backend abstraction.

These tests don't actually invoke any CLI: they exercise the dispatch
logic, JSON extraction, and error paths via monkeypatched ``run_cli``.
"""

from __future__ import annotations

from jarlis.ai import (
    AIBackend,
    AIError,
    AINotInstalled,
    ClaudeBackend,
    CodexBackend,
    GeminiBackend,
    LLMBackend,
    extract_json,
    get_backend,
)
from jarlis.ai import base as ai_base
from jarlis.config import Config


def _cfg(backend: str, model: str = "") -> Config:
    cfg = Config()
    cfg.ai.backend = backend
    cfg.ai.model = model
    return cfg


# ---------- get_backend dispatch ------------------------------------------


def test_get_backend_claude() -> None:
    b = get_backend(_cfg("claude"))
    assert isinstance(b, ClaudeBackend)
    assert b.name == "claude"


def test_get_backend_codex_gemini_llm() -> None:
    assert isinstance(get_backend(_cfg("codex")), CodexBackend)
    assert isinstance(get_backend(_cfg("gemini")), GeminiBackend)
    assert isinstance(get_backend(_cfg("llm", "gpt-4o-mini")), LLMBackend)


def test_get_backend_llm_requires_model() -> None:
    try:
        get_backend(_cfg("llm"))
    except AIError as e:
        assert "model" in str(e).lower()
    else:
        raise AssertionError("expected AIError when llm backend has no model")


def test_get_backend_unknown_raises() -> None:
    try:
        get_backend(_cfg("madeup"))
    except AIError as e:
        assert "madeup" in str(e)
    else:
        raise AssertionError("expected AIError for unknown backend")


def test_backends_implement_protocol() -> None:
    for b in (
        ClaudeBackend(),
        CodexBackend(),
        GeminiBackend(),
        LLMBackend(model="x"),
    ):
        assert isinstance(b, AIBackend)


# ---------- extract_json --------------------------------------------------


def test_extract_json_plain_object() -> None:
    obj = extract_json('garbage before {"a": 1, "b": [1,2,3]} garbage after')
    assert obj == {"a": 1, "b": [1, 2, 3]}


def test_extract_json_fenced() -> None:
    raw = "intro\n```json\n{\"x\": 42}\n```\ntrailing"
    assert extract_json(raw) == {"x": 42}


def test_extract_json_nested_braces() -> None:
    raw = 'noise {"outer": {"inner": {"deep": true}}} more noise'
    assert extract_json(raw) == {"outer": {"inner": {"deep": True}}}


def test_extract_json_no_object_raises() -> None:
    try:
        extract_json("no json here at all")
    except AIError:
        return
    raise AssertionError("expected AIError for missing JSON")


def test_extract_json_unbalanced_raises() -> None:
    try:
        extract_json('{"a": 1')
    except AIError:
        return
    raise AssertionError("expected AIError for unbalanced braces")


# ---------- subprocess wiring (mocked) ------------------------------------


def test_call_text_invokes_run_cli(monkeypatch_storage: dict) -> None:
    """Mock run_cli so we can verify command shape without a real CLI."""

    captured: dict = {}

    def fake_run_cli(cmd, *, stdin=None, timeout, max_retries, cwd, **kw):
        captured["cmd"] = cmd
        captured["stdin"] = stdin
        captured["cwd"] = cwd
        return "  hello world  \n"

    def fake_require_cli(_name):
        return None

    monkeypatch_storage["orig_run_cli"] = ai_base.run_cli
    monkeypatch_storage["orig_require"] = ai_base.require_cli
    ai_base.run_cli = fake_run_cli  # type: ignore[assignment]
    ai_base.require_cli = fake_require_cli  # type: ignore[assignment]

    try:
        b = ClaudeBackend(cwd="/tmp")
        out = b.call_text("PROMPT_PAYLOAD")
        assert out == "hello world"
        assert captured["cmd"][0] == "claude"
        assert "--allowedTools" in captured["cmd"]
        assert "Read" in captured["cmd"]
        # Settings JSON scoping Read to the project dir is passed too.
        assert "--settings" in captured["cmd"]
        idx = captured["cmd"].index("--settings")
        import json as _json
        settings = _json.loads(captured["cmd"][idx + 1])
        assert any("/tmp" in p for p in settings["permissions"]["allow"])
        assert any("ssh" in p for p in settings["permissions"]["deny"])
        assert captured["stdin"] == "PROMPT_PAYLOAD"
        assert captured["cwd"] == "/tmp"
    finally:
        ai_base.run_cli = monkeypatch_storage["orig_run_cli"]  # type: ignore[assignment]
        ai_base.require_cli = monkeypatch_storage["orig_require"]  # type: ignore[assignment]


def test_claude_use_read_tool_false_disables_read(monkeypatch_storage: dict) -> None:
    """When use_read_tool=False, Read tool isn't allowed and Edit/Write/Bash are denied."""
    captured: dict = {}

    def fake_run_cli(cmd, *, stdin=None, timeout, max_retries, cwd, **kw):
        captured["cmd"] = cmd
        return ""

    def fake_require_cli(_name):
        return None

    monkeypatch_storage["orig_run_cli"] = ai_base.run_cli
    monkeypatch_storage["orig_require"] = ai_base.require_cli
    ai_base.run_cli = fake_run_cli  # type: ignore[assignment]
    ai_base.require_cli = fake_require_cli  # type: ignore[assignment]
    try:
        ClaudeBackend(cwd="/tmp", use_read_tool=False).call_text("hi")
        assert "--allowedTools" not in captured["cmd"]
        assert "--settings" not in captured["cmd"]
        assert "--disallowedTools" in captured["cmd"]
        assert "Read" in captured["cmd"]  # appears as a denied tool now
        assert "Edit" in captured["cmd"]
        assert "Write" in captured["cmd"]
        assert "Bash" in captured["cmd"]
    finally:
        ai_base.run_cli = monkeypatch_storage["orig_run_cli"]  # type: ignore[assignment]
        ai_base.require_cli = monkeypatch_storage["orig_require"]  # type: ignore[assignment]


def test_codex_uses_read_only_sandbox(monkeypatch_storage: dict) -> None:
    captured: dict = {}

    def fake_run_cli(cmd, *, stdin=None, timeout, max_retries, cwd, **kw):
        captured["cmd"] = cmd
        return ""

    def fake_require_cli(_name):
        return None

    monkeypatch_storage["orig_run_cli"] = ai_base.run_cli
    monkeypatch_storage["orig_require"] = ai_base.require_cli
    ai_base.run_cli = fake_run_cli  # type: ignore[assignment]
    ai_base.require_cli = fake_require_cli  # type: ignore[assignment]

    try:
        CodexBackend().call_text("hi")
        assert "--sandbox" in captured["cmd"]
        idx = captured["cmd"].index("--sandbox")
        assert captured["cmd"][idx + 1] == "read-only"
    finally:
        ai_base.run_cli = monkeypatch_storage["orig_run_cli"]  # type: ignore[assignment]
        ai_base.require_cli = monkeypatch_storage["orig_require"]  # type: ignore[assignment]


def test_require_cli_raises_when_missing() -> None:
    try:
        ai_base.require_cli("definitely_not_a_real_cli_zzzqqq")
    except AINotInstalled:
        return
    raise AssertionError("expected AINotInstalled")


# ---------- model selection ----------------------------------------------


def _patch_run_cli(monkeypatch_storage: dict, captured: dict):
    """Helper: stub out run_cli/require_cli, capture argv into ``captured``."""

    def fake_run_cli(cmd, *, stdin=None, timeout, max_retries, cwd, **kw):
        captured["cmd"] = cmd
        captured["stdin"] = stdin
        return ""

    def fake_require_cli(_name):
        return None

    monkeypatch_storage["orig_run_cli"] = ai_base.run_cli
    monkeypatch_storage["orig_require"] = ai_base.require_cli
    ai_base.run_cli = fake_run_cli  # type: ignore[assignment]
    ai_base.require_cli = fake_require_cli  # type: ignore[assignment]


def _restore(monkeypatch_storage: dict) -> None:
    ai_base.run_cli = monkeypatch_storage["orig_run_cli"]  # type: ignore[assignment]
    ai_base.require_cli = monkeypatch_storage["orig_require"]  # type: ignore[assignment]


def test_claude_appends_model_flag_when_set(monkeypatch_storage: dict) -> None:
    captured: dict = {}
    _patch_run_cli(monkeypatch_storage, captured)
    try:
        ClaudeBackend(cwd="/tmp", model="sonnet").call_text("hi")
        assert "--model" in captured["cmd"]
        idx = captured["cmd"].index("--model")
        assert captured["cmd"][idx + 1] == "sonnet"
    finally:
        _restore(monkeypatch_storage)


def test_claude_omits_model_flag_when_unset(monkeypatch_storage: dict) -> None:
    captured: dict = {}
    _patch_run_cli(monkeypatch_storage, captured)
    try:
        ClaudeBackend(cwd="/tmp").call_text("hi")
        assert "--model" not in captured["cmd"]
    finally:
        _restore(monkeypatch_storage)


def test_codex_appends_model_flag_when_set(monkeypatch_storage: dict) -> None:
    captured: dict = {}
    _patch_run_cli(monkeypatch_storage, captured)
    try:
        CodexBackend(model="gpt-5-mini").call_text("hi")
        assert "--model" in captured["cmd"]
        idx = captured["cmd"].index("--model")
        assert captured["cmd"][idx + 1] == "gpt-5-mini"
        # And stdin marker still last
        assert captured["cmd"][-1] == "-"
    finally:
        _restore(monkeypatch_storage)


def test_gemini_uses_p_flag_for_prompt_and_model_short_form(monkeypatch_storage: dict) -> None:
    """Gemini's documented headless contract: prompt via -p, model via -m, no stdin."""
    captured: dict = {}
    _patch_run_cli(monkeypatch_storage, captured)
    try:
        GeminiBackend(model="gemini-2.5-flash").call_text("PROMPT_BODY")
        assert "-m" in captured["cmd"]
        idx_m = captured["cmd"].index("-m")
        assert captured["cmd"][idx_m + 1] == "gemini-2.5-flash"
        assert "-p" in captured["cmd"]
        idx_p = captured["cmd"].index("-p")
        assert captured["cmd"][idx_p + 1] == "PROMPT_BODY"
        # Stdin not used; gemini's documented pattern carries the prompt via -p.
        assert captured["stdin"] is None
    finally:
        _restore(monkeypatch_storage)


def test_gemini_json_call_appends_output_format(monkeypatch_storage: dict) -> None:
    captured: dict = {}
    _patch_run_cli(monkeypatch_storage, captured)
    try:
        b = GeminiBackend(model="gemini-2.5-pro")
        # call_json runs extract_json on output; stub a JSON return.
        ai_base.run_cli = lambda *a, **kw: '{"ok": true}'  # type: ignore[assignment]
        # Re-patch run_cli to also capture argv:
        def fake(cmd, *, stdin=None, timeout, max_retries, cwd, **kw):
            captured["cmd"] = cmd
            return '{"ok": true}'
        ai_base.run_cli = fake  # type: ignore[assignment]
        out = b.call_json("ask")
        assert out == {"ok": True}
        assert "--output-format" in captured["cmd"]
        idx = captured["cmd"].index("--output-format")
        assert captured["cmd"][idx + 1] == "json"
    finally:
        _restore(monkeypatch_storage)


def test_get_backend_resolves_draft_model_for_draft_task() -> None:
    cfg = _cfg("claude", model="sonnet")
    cfg.ai.model_draft = "opus"
    default_b = get_backend(cfg)
    draft_b = get_backend(cfg, task="draft")
    assert default_b.model == "sonnet"
    assert draft_b.model == "opus"


def test_get_backend_draft_falls_back_to_default_model_when_unset() -> None:
    cfg = _cfg("claude", model="sonnet")
    # cfg.ai.model_draft is left empty.
    draft_b = get_backend(cfg, task="draft")
    assert draft_b.model == "sonnet"


def test_get_backend_no_model_set_passes_none() -> None:
    """When neither default nor draft model is set, the CLI's own default is used (model=None)."""
    cfg = _cfg("claude")  # model="" by default
    b = get_backend(cfg)
    assert b.model is None
    b2 = get_backend(cfg, task="draft")
    assert b2.model is None
