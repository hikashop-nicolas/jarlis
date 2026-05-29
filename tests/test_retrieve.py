"""Tests for retrieve.py: structured retrieval over processed/ folder."""

from __future__ import annotations

import json
import tempfile
from datetime import datetime
from pathlib import Path

from jarlis import memory, retrieve
from jarlis.config import Config, init_paths
from jarlis.models import Email


def _make_cfg(tmp: Path) -> Config:
    cfg = Config()
    cfg.project_root = tmp
    init_paths(cfg)
    memory.ensure_layout(cfg)
    return cfg


def _store_processed(
    history_root: Path,
    *,
    folder_name: str,
    sender: str,
    subject: str,
    date: str,
    body: str,
    message_id: str = "",
    in_reply_to: str = "",
    references: list[str] | None = None,
    topic_slugs: list[str] | None = None,
) -> Path:
    folder = history_root / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    meta = {
        "message_id": message_id,
        "sender": sender,
        "subject": subject,
        "date": date,
        "in_reply_to": in_reply_to,
        "references": references or [],
        "classification": {"topic_slugs": topic_slugs or []},
    }
    (folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (folder / "body.txt").write_text(body, encoding="utf-8")
    return folder


def test_retrieve_picks_last_n_from_same_sender() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        root = cfg.processed_dir
        for i in range(7):
            _store_processed(
                root,
                folder_name=f"f{i:02d}",
                sender="bob@x.com",
                subject=f"thread {i}",
                date=f"2026-04-{i + 1:02d}T10:00",
                body=f"body {i}",
            )
        e = Email(message_id="<new@x>", sender="bob@x.com", sender_name="Bob")
        msgs = retrieve.retrieve_context(cfg, e, sender_n=3)
        assert len(msgs) == 3
        # Most recent first
        assert msgs[0].date >= msgs[-1].date
        assert all(m.source == "same-sender" for m in msgs)


def test_thread_walk_via_in_reply_to() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        root = cfg.processed_dir
        _store_processed(
            root,
            folder_name="parent",
            sender="alice@x.com",
            subject="Original",
            date="2026-04-01T10:00",
            body="root msg",
            message_id="<root@x>",
        )
        e = Email(
            message_id="<reply@x>",
            sender="someone-else@y.com",
            sender_name="Other",
            in_reply_to="<root@x>",
        )
        msgs = retrieve.retrieve_context(cfg, e)
        assert any(m.source == "thread" and m.subject == "Original" for m in msgs)


def test_topic_examples_when_subject_keywords_match() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        # Define a topic with matching keywords
        memory.save_topic(
            cfg,
            "grants",
            "# Topic\n\n**Subject keywords**: grant, subvention\n",
        )
        # Store a past email tagged with that slug
        _store_processed(
            cfg.processed_dir,
            folder_name="past_grant",
            sender="foundation@x.org",
            subject="Grant 2025: receipt",
            date="2025-11-01T10:00",
            body="thanks for your grant submission",
            topic_slugs=["grants"],
        )
        e = Email(
            message_id="<new@x>",
            sender="foundation@x.org",
            subject="Grant 2026: application",
            body_text="please send your grant application",
        )
        msgs = retrieve.retrieve_context(cfg, e)
        # Could match via same-sender too; what matters is the past_grant entry surfaces.
        assert any(m.subject == "Grant 2025: receipt" for m in msgs)


def test_render_for_prompt_formats_correctly() -> None:
    msgs = [
        retrieve.RetrievedMessage(
            source="same-sender",
            sender="bob@x.com",
            subject="hello",
            date="2026-04-01",
            snippet="body content here",
        ),
    ]
    rendered = retrieve.render_for_prompt(msgs)
    assert "Past similar exchanges" in rendered
    assert "[same-sender]" in rendered
    assert "bob@x.com" in rendered
    assert "body content here" in rendered


def test_render_for_prompt_empty_returns_empty_string() -> None:
    assert retrieve.render_for_prompt([]) == ""


def test_total_budget_is_respected() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        for i in range(20):
            _store_processed(
                cfg.processed_dir,
                folder_name=f"f{i:02d}",
                sender="bob@x.com",
                subject=f"t{i}",
                date=f"2026-01-{i + 1:02d}T10:00",
                body="x",
            )
        e = Email(message_id="<new@x>", sender="bob@x.com", sender_name="Bob")
        msgs = retrieve.retrieve_context(cfg, e, sender_n=20, total_budget=5)
        assert len(msgs) == 5
