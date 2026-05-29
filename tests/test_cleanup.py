"""Tests for cleanup.py."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from jarlis import cleanup, memory
from jarlis.config import Config, init_paths
from jarlis.models import (
    ARCHIVE_IGNORED_TOPIC,
    BUCKET_ARCHIVE,
    BUCKET_DRAFTED,
    BUCKET_FLAGGED,
    Classification,
)


def _make_cfg(tmp: Path) -> Config:
    cfg = Config()
    cfg.project_root = tmp
    init_paths(cfg)
    memory.ensure_layout(cfg)
    cfg.processed_dir.mkdir(parents=True, exist_ok=True)
    cfg.archived_dir.mkdir(parents=True, exist_ok=True)
    cfg.cleanup.enabled = True
    return cfg


def _drop_processed(
    parent: Path,
    *,
    name: str,
    sender: str,
    subject: str,
    body: str = "",
    date_iso: str | None = None,
    bucket: str = BUCKET_FLAGGED,
    archive_reason: str | None = None,
    topic_slugs: list[str] | None = None,
) -> Path:
    folder = parent / name
    folder.mkdir(parents=True, exist_ok=True)
    cls = Classification(
        bucket=bucket,
        archive_reason=archive_reason,
        topic_slugs=topic_slugs or [],
        reason="test",
    )
    meta = {
        "sender": sender,
        "sender_name": sender.split("@")[0],
        "subject": subject,
        "date": date_iso or "2026-04-01T10:00:00",
        "classification": cls.to_dict(),
    }
    (folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    if body:
        (folder / "body.txt").write_text(body, encoding="utf-8")
    return folder


# ---------- re-classification --------------------------------------------


def test_reclassify_moves_matched_to_archived() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        memory.save_section(
            cfg, "ignored_topics",
            "- Library cleanup (keywords: library, cleanup)\n",
        )
        _drop_processed(
            cfg.processed_dir,
            name="library1",
            sender="lib@x",
            subject="Library cleanup this Sat",
            body="please come",
            bucket=BUCKET_FLAGGED,
        )
        _drop_processed(
            cfg.processed_dir,
            name="grant1",
            sender="foundation@x",
            subject="Grant application",
            body="please send budget",
            bucket=BUCKET_DRAFTED,
        )
        report = cleanup.run_cleanup(cfg)
        assert "library1" in report.re_classified
        assert "grant1" not in report.re_classified
        # library1 moved
        assert not (cfg.processed_dir / "library1").exists()
        assert (cfg.archived_dir / "library1").exists()
        meta = json.loads((cfg.archived_dir / "library1" / "meta.json").read_text(encoding="utf-8"))
        assert meta["classification"]["bucket"] == BUCKET_ARCHIVE
        assert meta["classification"]["archive_reason"] == ARCHIVE_IGNORED_TOPIC


# ---------- people aging --------------------------------------------------


def test_age_out_people_archives_inactive() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.cleanup.people_archive_days = 30
        memory.save_person(cfg, "stale@x.com", "old contact")
        memory.save_person(cfg, "active@x.com", "active contact")

        # Active contact has a recent email
        recent = (date.today() - timedelta(days=5)).isoformat()
        _drop_processed(
            cfg.processed_dir, name="recent",
            sender="active@x.com", subject="recent", date_iso=recent + "T10:00:00",
        )
        # Stale contact has an old email
        old = (date.today() - timedelta(days=100)).isoformat()
        _drop_processed(
            cfg.processed_dir, name="old",
            sender="stale@x.com", subject="old", date_iso=old + "T10:00:00",
        )

        report = cleanup.run_cleanup(cfg)
        # Stale archived
        assert "stale_at_x_com" in report.people_archived
        assert (cfg.memory_dir / "archive" / "people" / "stale_at_x_com.md").exists()
        # Active stays
        assert (cfg.memory_dir / "people" / "active_at_x_com.md").exists()


# ---------- topics aging --------------------------------------------------


def test_age_out_topics_archives_inactive() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.cleanup.topic_archive_days = 30
        memory.save_topic(cfg, "active_topic", "active topic content")
        memory.save_topic(cfg, "stale_topic", "stale topic content")

        recent = (date.today() - timedelta(days=5)).isoformat()
        _drop_processed(
            cfg.processed_dir, name="r1",
            sender="x@x", subject="active subject",
            date_iso=recent + "T10:00:00",
            topic_slugs=["active_topic"],
        )
        old = (date.today() - timedelta(days=100)).isoformat()
        _drop_processed(
            cfg.processed_dir, name="o1",
            sender="x@x", subject="old subject",
            date_iso=old + "T10:00:00",
            topic_slugs=["stale_topic"],
        )

        report = cleanup.run_cleanup(cfg)
        assert "stale_topic" in report.topics_archived
        assert "active_topic" not in report.topics_archived
        assert (cfg.memory_dir / "archive" / "topics" / "stale_topic.md").exists()
        assert (cfg.memory_dir / "topics" / "active_topic.md").exists()


# ---------- body retention -----------------------------------------------


def test_old_bodies_are_deleted_meta_kept() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.cleanup.processed_email_keep_days = 30
        old = (date.today() - timedelta(days=100)).isoformat()
        folder = _drop_processed(
            cfg.processed_dir, name="ancient",
            sender="x@x", subject="ancient",
            body="this body should be wiped",
            date_iso=old + "T10:00:00",
        )
        report = cleanup.run_cleanup(cfg)
        assert "ancient" in report.bodies_deleted
        assert not (folder / "body.txt").exists()
        assert (folder / "meta.json").exists()
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        assert "body_deleted_on" in meta


def test_keep_forever_when_zero_days() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.cleanup.processed_email_keep_days = 0
        old = (date.today() - timedelta(days=1000)).isoformat()
        folder = _drop_processed(
            cfg.processed_dir, name="fossil",
            sender="x@x", subject="fossil",
            body="ancient body",
            date_iso=old + "T10:00:00",
        )
        report = cleanup.run_cleanup(cfg)
        assert report.bodies_deleted == []
        assert (folder / "body.txt").exists()


def test_run_cleanup_disabled_does_nothing() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.cleanup.enabled = False
        memory.save_person(cfg, "stale@x.com", "x")
        report = cleanup.run_cleanup(cfg)
        assert report.people_archived == []
        assert report.topics_archived == []
        # File still there
        assert (cfg.memory_dir / "people" / "stale_at_x_com.md").exists()


def test_archive_old_drafts_moves_aged_out_of_queue() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.cleanup.draft_pending_days = 14
        cfg.queue_dir.mkdir(parents=True, exist_ok=True)

        old = cfg.queue_dir / "20260508_old.md"
        old.write_text("# Draft\nTo: a@x\nSubject: Old\n", encoding="utf-8")
        ancient = (datetime.now() - timedelta(days=30)).timestamp()
        os.utime(old, (ancient, ancient))

        fresh = cfg.queue_dir / "20260528_fresh.md"
        fresh.write_text("# Draft\nTo: b@x\nSubject: Fresh\n", encoding="utf-8")

        report = cleanup.run_cleanup(cfg)

        assert report.drafts_archived == ["20260508_old.md"]
        # Aged draft moved into archived/, fresh one stays in the queue.
        assert not old.exists()
        assert (cfg.queue_dir / "archived" / "20260508_old.md").exists()
        assert fresh.exists()
        # Non-recursive glob means the archived draft no longer counts as active.
        assert list(cfg.queue_dir.glob("*.md")) == [fresh]
