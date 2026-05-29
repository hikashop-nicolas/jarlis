"""Tests for translation.py."""

from __future__ import annotations

import tempfile
from pathlib import Path

from jarlis import translation
from jarlis.config import Config, init_paths


def _make_cfg(tmp: Path, *, target_lang: str = "fr") -> Config:
    cfg = Config()
    cfg.project_root = tmp
    init_paths(cfg)
    cfg.user.languages = [target_lang]
    return cfg


class _Stub:
    name = "stub"

    def __init__(self, response: str = "STUB_TRANSLATION") -> None:
        self.response = response
        self.calls: list[str] = []

    def call_text(self, prompt: str) -> str:
        self.calls.append(prompt)
        return self.response

    def call_json(self, prompt: str) -> dict:
        return {}


def test_translate_returns_none_for_empty_input() -> None:
    backend = _Stub("anything")
    assert translation.translate("", source_lang="fr", target_lang="en", backend=backend) is None
    assert backend.calls == []


def test_translate_returns_none_when_source_equals_target() -> None:
    backend = _Stub("anything")
    out = translation.translate("hello", source_lang="en", target_lang="en", backend=backend)
    assert out is None
    assert backend.calls == []


def test_translate_calls_backend_with_security_notice() -> None:
    backend = _Stub("Bonjour le monde")
    out = translation.translate("Hello world", source_lang="en", target_lang="fr", backend=backend)
    assert out == "Bonjour le monde"
    assert len(backend.calls) == 1
    prompt = backend.calls[0]
    assert "Hello world" in prompt
    assert "UNTRUSTED" in prompt
    assert "fr" in prompt
    assert "en" in prompt


def test_translate_truncates_long_input() -> None:
    backend = _Stub("ok")
    long_text = "A" * (translation.MAX_INPUT_CHARS + 100)
    translation.translate(long_text, source_lang="en", target_lang="fr", backend=backend)
    prompt = backend.calls[0]
    assert "truncated by JARLIS" in prompt
    # The prompt should not contain the full original (capped)
    assert prompt.count("A") <= translation.MAX_INPUT_CHARS + 50


def test_summarize_includes_security_notice() -> None:
    backend = _Stub("This is a 3-sentence summary.")
    out = translation.summarize("Long email text", target_lang="fr", backend=backend)
    assert out == "This is a 3-sentence summary."
    prompt = backend.calls[0]
    assert "UNTRUSTED" in prompt
    assert "fr" in prompt


def test_maybe_translate_original_skips_when_disabled() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.translation.translate_original = False
        backend = _Stub()
        out = translation.maybe_translate_original(cfg, "Hi", "en", backend)
        assert out is None
        assert backend.calls == []


def test_maybe_translate_original_skips_when_languages_match() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t), target_lang="en")
        backend = _Stub()
        out = translation.maybe_translate_original(cfg, "Hi", "en", backend)
        assert out is None


def test_maybe_translate_original_runs_when_languages_differ() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t), target_lang="fr")
        backend = _Stub("Bonjour")
        out = translation.maybe_translate_original(cfg, "Hello", "en", backend)
        assert out == "Bonjour"
        assert len(backend.calls) == 1


def test_maybe_translate_draft_skips_when_languages_match() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t), target_lang="ja")
        backend = _Stub()
        out = translation.maybe_translate_draft(cfg, "draft", "ja", backend)
        assert out is None


def test_maybe_translate_draft_runs_when_languages_differ() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t), target_lang="fr")
        backend = _Stub("Voici la traduction")
        out = translation.maybe_translate_draft(cfg, "draft body", "ja", backend)
        assert out == "Voici la traduction"


def test_maybe_summarize_skips_short_bodies() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.translation.summarize_above_chars = 1000
        backend = _Stub()
        out = translation.maybe_summarize(cfg, "short", backend)
        assert out is None


def test_maybe_summarize_runs_when_body_exceeds_threshold() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.translation.summarize_above_chars = 100
        backend = _Stub("Bob asks about budget. Deadline next week.")
        out = translation.maybe_summarize(cfg, "A" * 500, backend)
        assert "Bob asks about budget" in (out or "")


def test_maybe_summarize_zero_disables() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.translation.summarize_above_chars = 0
        backend = _Stub()
        out = translation.maybe_summarize(cfg, "A" * 100_000, backend)
        assert out is None


def test_maybe_translate_original_no_backend_returns_none() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        out = translation.maybe_translate_original(cfg, "Hi", "en", backend=None)
        assert out is None
