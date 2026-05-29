"""Tests for the cascading classifier."""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

from jarlis import classify, memory
from jarlis.config import Config, init_paths
from jarlis.models import (
    ARCHIVE_IGNORED_TOPIC,
    ARCHIVE_SPAM,
    BUCKET_ARCHIVE,
    BUCKET_DRAFTED,
    BUCKET_FLAGGED,
    LAYER_CACHE,
    LAYER_LLM,
    LAYER_RULES,
    Email,
)


def _make_cfg(tmp: Path) -> Config:
    cfg = Config()
    cfg.project_root = tmp
    init_paths(cfg)
    memory.ensure_layout(cfg)
    return cfg


def _email(**kw) -> Email:
    defaults = dict(
        message_id="<msg-1@x>",
        sender="alice@example.com",
        sender_name="Alice",
        subject="Hello",
        body_text="hi there",
        date=datetime(2026, 5, 7, 10, 0),
    )
    defaults.update(kw)
    return Email(**defaults)


# ---------- layer 2: rules -----------------------------------------------


def test_spam_pattern_in_sender_routes_to_archive_spam() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        e = _email(sender="mailer-daemon@gmail.com", subject="Delivery Status Notification")
        cls = classify.classify_email(cfg, e)
        assert cls.bucket == BUCKET_ARCHIVE
        assert cls.archive_reason == ARCHIVE_SPAM
        assert cls.layer == LAYER_RULES


def test_spam_subject_marker_routes_to_archive_spam() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        e = _email(sender="random@example.com", subject="Promotional offer for you")
        cls = classify.classify_email(cfg, e)
        assert cls.bucket == BUCKET_ARCHIVE
        assert cls.archive_reason == ARCHIVE_SPAM


def test_ignored_topic_routes_to_archive_ignored() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        memory.save_section(
            cfg,
            "ignored_topics",
            "- Library cleanup announcements (keywords: library, cleanup)\n",
        )
        e = _email(subject="Library cleanup this Saturday", body_text="please come help")
        cls = classify.classify_email(cfg, e)
        assert cls.bucket == BUCKET_ARCHIVE
        assert cls.archive_reason == ARCHIVE_IGNORED_TOPIC
        assert cls.topic_slugs == ["library_cleanup_announcements"]
        assert cls.layer == LAYER_RULES
        assert cls.why_log[-1].layer == LAYER_RULES


def test_no_rule_match_and_no_backend_returns_flagged() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        e = _email(subject="Need your input on grant application")
        cls = classify.classify_email(cfg, e)
        # No backend wired → flagged-by-default
        assert cls.bucket == BUCKET_FLAGGED
        assert cls.confidence == 0.0


# ---------- layer 1: cache ------------------------------------------------


def test_cache_hit_returns_prior_classification() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cache_path = Path(t) / "seen.json"
        e = _email(message_id="<msg-cached@x>")

        # First call (no cache, no backend) → flagged-by-default
        cls1 = classify.classify_email(cfg, e, cache_path=cache_path)
        # Pretend it became a 'drafted' decision and persist
        cls1.bucket = BUCKET_DRAFTED
        cls1.reason = "user marked as drafted"
        classify.update_cache(cache_path, e, cls1)

        # Second call should hit cache.
        cls2 = classify.classify_email(cfg, e, cache_path=cache_path)
        assert cls2.bucket == BUCKET_DRAFTED
        assert cls2.layer == LAYER_CACHE
        assert any(entry.layer == LAYER_CACHE for entry in cls2.why_log)


# ---------- layer 3: LLM (with stub backend) -----------------------------


class _StubBackend:
    name = "stub"

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.called_with: str = ""

    def call_text(self, prompt: str) -> str:
        return ""

    def call_json(self, prompt: str) -> dict:
        self.called_with = prompt
        return self.payload


def test_llm_layer_returns_drafted() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        backend = _StubBackend({
            "bucket": "drafted",
            "archive_reason": None,
            "topic_slugs": ["grants"],
            "reason": "Grant application requires a budget reply.",
        })
        e = _email(subject="Grant application: budget question")
        cls = classify.classify_email(cfg, e, backend=backend)
        assert cls.bucket == BUCKET_DRAFTED
        assert cls.layer == LAYER_LLM
        assert "grants" in cls.topic_slugs
        assert "budget" in cls.reason.lower()


def test_llm_invalid_bucket_falls_back_to_flagged() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        backend = _StubBackend({
            "bucket": "garbage",
            "archive_reason": None,
            "topic_slugs": [],
            "reason": "...",
        })
        e = _email()
        cls = classify.classify_email(cfg, e, backend=backend)
        assert cls.bucket == BUCKET_FLAGGED
        assert cls.archive_reason is None


def test_llm_archive_reason_only_kept_when_archive() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        # Backend mistakenly returns archive_reason on a 'drafted' bucket.
        backend = _StubBackend({
            "bucket": "drafted",
            "archive_reason": "low_priority",
            "topic_slugs": [],
            "reason": "test",
        })
        cls = classify.classify_email(cfg, _email(), backend=backend)
        assert cls.bucket == BUCKET_DRAFTED
        assert cls.archive_reason is None  # cleared because bucket != archive


# ---------- topic keyword matching helper --------------------------------


def test_matched_topic_slugs_uses_subject_keywords_line() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        memory.save_topic(
            cfg,
            "grants",
            "# Topic: Grants\n\n**Subject keywords**: grant, subvention, funding\n",
        )
        e = _email(subject="Subvention 2026: application question")
        slugs = classify.matched_topic_slugs(cfg, e)
        assert "grants" in slugs
