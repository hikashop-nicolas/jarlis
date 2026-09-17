"""Tests for maintenance.py: log rotation, cache pruning, pending compaction."""

from __future__ import annotations

import gzip
import json
import tempfile
from datetime import date, timedelta
from pathlib import Path

from jarlis import maintenance
from jarlis.config import Config, init_paths


def _make_cfg(tmp: Path) -> Config:
    cfg = Config()
    cfg.project_root = tmp
    init_paths(cfg)
    cfg.processed_dir.mkdir(parents=True, exist_ok=True)
    cfg.archived_dir.mkdir(parents=True, exist_ok=True)
    cfg.attachments_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _drop_email(cfg: Config, name: str, message_id: str) -> None:
    folder = cfg.processed_dir / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "meta.json").write_text(
        json.dumps({"message_id": message_id, "sender": "a@b.com", "date": "2026-09-01T10:00:00"}),
        encoding="utf-8",
    )


class _FakeBackend:
    def __init__(self, reply: str = "- un point\n- un autre") -> None:
        self.reply = reply
        self.prompts: list[str] = []

    def call_text(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.reply

    def call_json(self, prompt: str) -> dict:
        return {}


# ---------- log rotation -------------------------------------------------


def test_rotate_logs_compresses_and_truncates_in_place() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        big = cfg.project_root / "com.jarlis.pipeline.log"
        big.write_text("x" * 2_000_000, encoding="utf-8")
        small = cfg.project_root / "com.jarlis.cleanup.log"
        small.write_text("tiny", encoding="utf-8")

        rotated = maintenance.rotate_logs(cfg, max_mb=1, keep=3)

        assert rotated == ["com.jarlis.pipeline.log"]
        gz = cfg.project_root / "com.jarlis.pipeline.log.1.gz"
        assert gz.exists()
        with gzip.open(gz, "rt", encoding="utf-8") as f:
            assert len(f.read()) == 2_000_000
        assert big.exists() and big.stat().st_size == 0
        assert small.read_text(encoding="utf-8") == "tiny"


def test_rotate_logs_keeps_only_n_generations() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        log = cfg.project_root / "a.log"
        for _ in range(4):
            log.write_text("y" * 2_000_000, encoding="utf-8")
            maintenance.rotate_logs(cfg, max_mb=1, keep=2)

        assert (cfg.project_root / "a.log.1.gz").exists()
        assert (cfg.project_root / "a.log.2.gz").exists()
        assert not (cfg.project_root / "a.log.3.gz").exists()


def test_rotate_logs_disabled_when_max_is_zero() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        (cfg.project_root / "a.log").write_text("x" * 100, encoding="utf-8")
        assert maintenance.rotate_logs(cfg, max_mb=0, keep=3) == []


# ---------- classifier cache ---------------------------------------------


def test_prune_classification_cache_drops_entries_with_no_email_left() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        _drop_email(cfg, "kept", "<alive@x>")
        cache = {"<alive@x>": {"bucket": "flagged"}, "<gone@x>": {"bucket": "archive"}}
        path = cfg.project_root / maintenance.CACHE_FILENAME
        path.write_text(json.dumps(cache), encoding="utf-8")

        removed = maintenance.prune_classification_cache(cfg)

        assert removed == 1
        assert json.loads(path.read_text()) == {"<alive@x>": {"bucket": "flagged"}}


def test_prune_classification_cache_refuses_to_empty_on_missing_history() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cache = {"<a@x>": {}, "<b@x>": {}}
        path = cfg.project_root / maintenance.CACHE_FILENAME
        path.write_text(json.dumps(cache), encoding="utf-8")

        assert maintenance.prune_classification_cache(cfg) == 0
        assert len(json.loads(path.read_text())) == 2


# ---------- pending_attention.md -----------------------------------------


def _pending(cfg: Config, days_old: int, recent_days: int = 1) -> None:
    old_day = (date.today() - timedelta(days=days_old)).isoformat()
    new_day = (date.today() - timedelta(days=recent_days)).isoformat()
    cfg.pending_attention_path.write_text(
        "# Pending attention\n\n"
        f"## {old_day}\n\n- 08:00: from a: vieux sujet\n- 09:00: from b: autre\n\n"
        f"## {new_day}\n\n- 10:00: from c: récent\n",
        encoding="utf-8",
    )


def test_compact_pending_attention_archives_old_months_only() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        _pending(cfg, days_old=120)

        months, lines, digests = maintenance.compact_pending_attention(cfg, keep_days=60)

        assert len(months) == 1
        assert lines == 2
        assert digests == []
        archive = cfg.pending_attention_archive_dir / f"{months[0]}.md"
        assert "vieux sujet" in archive.read_text(encoding="utf-8")
        remaining = cfg.pending_attention_path.read_text(encoding="utf-8")
        assert "vieux sujet" not in remaining
        assert "récent" in remaining
        assert remaining.startswith("# Pending attention")


def test_compact_pending_attention_writes_an_ai_digest_when_a_backend_is_given() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        _pending(cfg, days_old=120)
        backend = _FakeBackend()

        months, _lines, digests = maintenance.compact_pending_attention(
            cfg, keep_days=60, backend=backend
        )

        assert digests == months
        archive = (cfg.pending_attention_archive_dir / f"{months[0]}.md").read_text(encoding="utf-8")
        assert "## Digest" in archive
        assert "un point" in archive
        assert "vieux sujet" in backend.prompts[0]


def test_compact_pending_attention_survives_a_failing_backend() -> None:
    class Boom:
        def call_text(self, prompt: str) -> str:
            raise RuntimeError("cli down")

        def call_json(self, prompt: str) -> dict:
            return {}

    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        _pending(cfg, days_old=120)

        months, _lines, digests = maintenance.compact_pending_attention(
            cfg, keep_days=60, backend=Boom()
        )

        assert months and digests == []
        assert "vieux sujet" in (cfg.pending_attention_archive_dir / f"{months[0]}.md").read_text()


def test_compact_pending_attention_noop_when_nothing_is_old_enough() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        _pending(cfg, days_old=10)
        before = cfg.pending_attention_path.read_text(encoding="utf-8")

        months, lines, _ = maintenance.compact_pending_attention(cfg, keep_days=60)

        assert (months, lines) == ([], 0)
        assert cfg.pending_attention_path.read_text(encoding="utf-8") == before


def test_compact_pending_attention_disabled_with_zero_keep_days() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        _pending(cfg, days_old=400)
        assert maintenance.compact_pending_attention(cfg, keep_days=0) == ([], 0, [])


# ---------- top level ----------------------------------------------------


def test_run_maintenance_touches_every_area() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.maintenance.log_max_mb = 1
        cfg.maintenance.pending_attention_keep_days = 60
        cfg.maintenance.use_ai = False

        (cfg.project_root / "a.log").write_text("z" * 2_000_000, encoding="utf-8")
        _drop_email(cfg, "kept", "<alive@x>")
        (cfg.project_root / maintenance.CACHE_FILENAME).write_text(
            json.dumps({"<alive@x>": {}, "<gone@x>": {}}), encoding="utf-8"
        )
        _pending(cfg, days_old=120)
        for folder in ("f1", "f2"):
            d = cfg.attachments_dir / folder
            d.mkdir(parents=True, exist_ok=True)
            (d / "dup.bin").write_bytes(b"q" * 2048)

        report = maintenance.run_maintenance(cfg)

        assert report.logs_rotated == ["a.log"]
        assert report.cache_entries_pruned == 1
        assert report.pending_months_archived
        assert report.attachments_linked == 1
        assert report.attachment_bytes_saved == 2048
        assert report.did_something is True


def test_run_maintenance_respects_disabled() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.maintenance.enabled = False
        (cfg.project_root / "a.log").write_text("z" * 2_000_000, encoding="utf-8")

        report = maintenance.run_maintenance(cfg)

        assert report.to_dict()["logs_rotated"] == []
        assert report.did_something is False


def test_is_due_today_matches_configured_day() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        cfg.maintenance.day_of_month = 12
        assert maintenance.is_due_today(cfg, date(2026, 10, 12)) is True
        assert maintenance.is_due_today(cfg, date(2026, 10, 13)) is False
        cfg.maintenance.enabled = False
        assert maintenance.is_due_today(cfg, date(2026, 10, 12)) is False
