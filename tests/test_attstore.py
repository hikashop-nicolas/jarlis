"""Tests for attstore.py: content-addressed attachments and dedup."""

from __future__ import annotations

import tempfile
from pathlib import Path

from jarlis import attstore
from jarlis.config import Config, init_paths


def _make_cfg(tmp: Path) -> Config:
    cfg = Config()
    cfg.project_root = tmp
    init_paths(cfg)
    cfg.attachments_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _store_files(cfg: Config) -> list[Path]:
    return [p for p in attstore.store_dir(cfg).rglob("*") if p.is_file()]


def test_same_bytes_in_two_emails_share_one_stored_copy() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        data = b"%PDF-1.4 same contract"

        a = attstore.save_attachment(cfg, "20260101_aaa", "contract.pdf", data)
        b = attstore.save_attachment(cfg, "20260202_bbb", "contract_copy.pdf", data)

        assert a.exists() and b.exists()
        assert a.read_bytes() == b.read_bytes() == data
        assert a.samefile(b)
        assert len([p for p in _store_files(cfg) if p.suffix == ".pdf"]) == 1


def test_different_bytes_stay_separate() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        a = attstore.save_attachment(cfg, "f1", "a.txt", b"one")
        b = attstore.save_attachment(cfg, "f2", "b.txt", b"two")
        assert not a.samefile(b)
        assert len(_store_files(cfg)) >= 2


def test_extracted_text_is_produced_once_and_linked_into_each_email() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        data = b"ligne une\nligne deux"

        a = attstore.save_attachment(cfg, "f1", "note.txt", data)
        b = attstore.save_attachment(cfg, "f2", "note.txt", data)

        sib_a = a.with_name(a.name + ".extracted.txt")
        sib_b = b.with_name(b.name + ".extracted.txt")
        assert sib_a.exists() and sib_b.exists()
        assert "ligne une" in sib_a.read_text(encoding="utf-8")
        assert sib_a.samefile(sib_b)


def test_dedup_existing_folds_legacy_copies_into_links() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        data = b"x" * 4096
        legacy = []
        for folder in ("f1", "f2", "f3"):
            d = cfg.attachments_dir / folder
            d.mkdir(parents=True, exist_ok=True)
            p = d / "report.pdf"
            p.write_bytes(data)
            legacy.append(p)

        report = attstore.dedup_existing(cfg)

        assert report.scanned == 3
        assert report.stored == 1
        assert report.linked == 2
        assert report.bytes_saved == 2 * 4096
        assert legacy[0].samefile(legacy[1]) and legacy[1].samefile(legacy[2])
        assert all(p.read_bytes() == data for p in legacy)


def test_dedup_existing_is_idempotent() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        for folder in ("f1", "f2"):
            d = cfg.attachments_dir / folder
            d.mkdir(parents=True, exist_ok=True)
            (d / "same.bin").write_bytes(b"payload")

        attstore.dedup_existing(cfg)
        second = attstore.dedup_existing(cfg)

        assert second.linked == 0
        assert second.stored == 0
        assert second.already_linked == 2


def test_dry_run_reports_duplicates_it_would_fold() -> None:
    """A dry run writes nothing, so it must track digests in memory: otherwise
    every copy finds an empty store and the report shows zero duplicates."""
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        data = b"z" * 1024
        for folder in ("f1", "f2", "f3"):
            d = cfg.attachments_dir / folder
            d.mkdir(parents=True, exist_ok=True)
            (d / "same.pdf").write_bytes(data)

        report = attstore.dedup_existing(cfg, dry_run=True)

        assert report.scanned == 3
        assert report.stored == 1
        assert report.linked == 2
        assert report.bytes_saved == 2048


def test_dry_run_changes_nothing() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        d = cfg.attachments_dir / "f1"
        d.mkdir(parents=True, exist_ok=True)
        (d / "a.bin").write_bytes(b"payload")

        report = attstore.dedup_existing(cfg, dry_run=True)

        assert report.stored == 1
        assert not attstore.store_dir(cfg).exists()


def test_gc_removes_unreferenced_store_entries_only() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        linked = attstore.save_attachment(cfg, "f1", "kept.bin", b"kept")
        orphan = attstore.save_attachment(cfg, "f2", "gone.bin", b"orphan")
        orphan.unlink()

        removed, freed = attstore.gc(cfg)

        assert removed == 1
        assert freed == len(b"orphan")
        assert linked.exists()


def test_gc_dry_run_keeps_everything() -> None:
    with tempfile.TemporaryDirectory() as t:
        cfg = _make_cfg(Path(t))
        orphan = attstore.save_attachment(cfg, "f1", "gone.bin", b"orphan")
        orphan.unlink()

        removed, _ = attstore.gc(cfg, dry_run=True)

        assert removed == 1
        assert len([p for p in _store_files(cfg) if p.name.endswith(".bin")]) == 1
