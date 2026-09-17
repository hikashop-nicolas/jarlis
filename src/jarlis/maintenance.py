"""Monthly housekeeping of JARLIS's own files.

``cleanup`` looks after the mailbox: contacts, topics, old bodies, stale
drafts. This module looks after everything JARLIS writes about itself,
which otherwise only ever grows:

  - logs in the project root (no rotation anywhere else)
  - ``seen_classifications.json``, one entry per email ever classified,
    read and rewritten on every single run
  - ``pending_attention.md``, appended to forever and never read back
  - duplicate attachment bytes (see :mod:`jarlis.attstore`)

Everything here is reversible or inert: logs are compressed rather than
deleted, pending entries move to a per-month archive file, and the
attachment pass only replaces duplicates with hard links to identical
bytes.

When an AI backend is available, each month archived out of
``pending_attention.md`` gets a short digest at the top of its archive
file, so a year of flagged-but-not-drafted mail stays skimmable. The
digest is a convenience: losing the backend loses the summary, never the
entries.
"""

from __future__ import annotations

import gzip
import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import attstore
from .ai import AIBackend, AIError
from .config import Config

log = logging.getLogger(__name__)

CACHE_FILENAME = "seen_classifications.json"

# "## 2026-05-08" headers inside pending_attention.md
_DATE_HEADING = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})\s*$")

_DIGEST_PROMPT = """You are summarizing one month of an email triage log.

Below are the entries JARLIS flagged for attention in {month}. Write a digest
of at most 8 bullet points in {lang}, grouping recurring threads together and
naming the people involved. Facts only, no advice.

Output the bullet list and nothing else: no title, no introduction, no closing
line. Start your answer with the first "-".

{entries}
"""


@dataclass
class MaintenanceReport:
    logs_rotated: list[str] = field(default_factory=list)
    cache_entries_pruned: int = 0
    pending_months_archived: list[str] = field(default_factory=list)
    pending_lines_archived: int = 0
    digests_written: list[str] = field(default_factory=list)
    attachments_linked: int = 0
    attachment_bytes_saved: int = 0
    store_files_removed: int = 0
    store_bytes_freed: int = 0

    def to_dict(self) -> dict:
        return {
            "logs_rotated": self.logs_rotated,
            "cache_entries_pruned": self.cache_entries_pruned,
            "pending_months_archived": self.pending_months_archived,
            "pending_lines_archived": self.pending_lines_archived,
            "digests_written": self.digests_written,
            "attachments_linked": self.attachments_linked,
            "attachment_bytes_saved": self.attachment_bytes_saved,
            "store_files_removed": self.store_files_removed,
            "store_bytes_freed": self.store_bytes_freed,
        }

    @property
    def did_something(self) -> bool:
        return bool(
            self.logs_rotated
            or self.cache_entries_pruned
            or self.pending_months_archived
            or self.attachments_linked
            or self.store_files_removed
        )


# ---------- 1. log rotation ----------------------------------------------


def rotate_logs(cfg: Config, *, max_mb: int, keep: int, dry_run: bool = False) -> list[str]:
    """Compress oversized logs in the project root, keeping ``keep`` of each.

    ``x.log`` becomes ``x.log.1.gz``, the previous ``.1.gz`` shifts to
    ``.2.gz``, and anything past ``keep`` is dropped. The live file is
    truncated rather than renamed, so a scheduler holding it open (launchd
    keeps the handle) keeps writing to the same inode.
    """
    if max_mb <= 0:
        return []
    limit = max_mb * 1024 * 1024
    rotated: list[str] = []
    for path in sorted(cfg.project_root.glob("*.log")):
        try:
            if path.stat().st_size < limit:
                continue
        except OSError:
            continue
        if dry_run:
            rotated.append(path.name)
            continue
        try:
            for i in range(keep, 0, -1):
                older = path.with_name(f"{path.name}.{i}.gz")
                if not older.exists():
                    continue
                if i == keep:
                    older.unlink()
                else:
                    older.replace(path.with_name(f"{path.name}.{i + 1}.gz"))
            with path.open("rb") as src, gzip.open(path.with_name(f"{path.name}.1.gz"), "wb") as dst:
                shutil.copyfileobj(src, dst)
            with path.open("w", encoding="utf-8"):
                pass  # truncate in place; the writer keeps its file handle
            rotated.append(path.name)
        except OSError as exc:
            log.warning("could not rotate %s: %s", path, exc)
    return rotated


# ---------- 2. classifier cache ------------------------------------------


def prune_classification_cache(cfg: Config, *, dry_run: bool = False) -> int:
    """Drop cache entries whose email is no longer on disk. Returns the count.

    The cache is keyed by Message-ID and exists to avoid re-classifying an
    email JARLIS has already seen. Once the email folder is gone, the entry
    can never be hit again.
    """
    from . import cleanup as _cleanup

    path = cfg.project_root / CACHE_FILENAME
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(cache, dict) or not cache:
        return 0

    live: set[str] = set()
    for _meta_path, meta in _cleanup._iter_metas(cfg):
        mid = (meta.get("message_id") or "").strip()
        if mid:
            live.add(mid)
    if not live:
        # No history on disk at all: refuse to empty the cache on what is
        # more likely a misconfigured path than a real deletion.
        return 0

    keep = {k: v for k, v in cache.items() if k in live}
    removed = len(cache) - len(keep)
    if removed and not dry_run:
        path.write_text(json.dumps(keep, ensure_ascii=False, indent=2), encoding="utf-8")
    return removed


# ---------- 3. pending_attention.md --------------------------------------


def _split_pending(text: str) -> tuple[str, list[tuple[str, list[str]]]]:
    """Split into (header, [(date, lines)]) preserving original formatting."""
    header: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    current: tuple[str, list[str]] | None = None
    for line in text.splitlines():
        m = _DATE_HEADING.match(line)
        if m:
            if current:
                sections.append(current)
            current = (m.group(1), [])
            continue
        if current is None:
            header.append(line)
        else:
            current[1].append(line)
    if current:
        sections.append(current)
    return ("\n".join(header).rstrip() + "\n", sections)


def compact_pending_attention(
    cfg: Config,
    *,
    keep_days: int,
    today: date | None = None,
    backend: AIBackend | None = None,
    dry_run: bool = False,
) -> tuple[list[str], int, list[str]]:
    """Move old entries into ``pending_attention_archive/<YYYY-MM>.md``.

    Returns ``(months_archived, lines_archived, digests_written)``.
    """
    if keep_days <= 0:
        return ([], 0, [])
    path = cfg.pending_attention_path
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ([], 0, [])

    today = today or date.today()
    cutoff = today - timedelta(days=keep_days)
    header, sections = _split_pending(text)
    if not sections:
        return ([], 0, [])

    keep: list[tuple[str, list[str]]] = []
    by_month: dict[str, list[tuple[str, list[str]]]] = {}
    archived_lines = 0
    for day, lines in sections:
        try:
            d = date.fromisoformat(day)
        except ValueError:
            keep.append((day, lines))
            continue
        if d >= cutoff:
            keep.append((day, lines))
            continue
        by_month.setdefault(day[:7], []).append((day, lines))
        archived_lines += sum(1 for ln in lines if ln.strip())

    if not by_month:
        return ([], 0, [])
    if dry_run:
        return (sorted(by_month), archived_lines, [])

    digests: list[str] = []
    archive_dir = cfg.pending_attention_archive_dir
    archive_dir.mkdir(parents=True, exist_ok=True)
    for month, months_sections in sorted(by_month.items()):
        body_parts = [f"## {day}\n" + "\n".join(lines).rstrip() for day, lines in months_sections]
        body = "\n\n".join(body_parts).rstrip() + "\n"
        digest = _digest_for(cfg, month, body, backend) if backend else None
        if digest:
            digests.append(month)
        out = archive_dir / f"{month}.md"
        chunks = [f"# Pending attention: {month}\n"]
        if digest:
            chunks.append("## Digest\n\n" + digest.strip() + "\n")
        chunks.append(body)
        existing = out.read_text(encoding="utf-8") if out.exists() else ""
        out.write_text(existing + "\n".join(chunks), encoding="utf-8")

    kept_text = header.rstrip() + "\n\n"
    kept_text += "\n\n".join(
        f"## {day}\n" + "\n".join(lines).rstrip() for day, lines in keep
    ).rstrip()
    path.write_text(kept_text + "\n", encoding="utf-8")
    return (sorted(by_month), archived_lines, digests)


def _digest_for(cfg: Config, month: str, body: str, backend: AIBackend) -> str | None:
    lang = (cfg.user.languages or ["en"])[0]
    prompt = _DIGEST_PROMPT.format(month=month, lang=lang, entries=body[:40_000])
    try:
        return backend.call_text(prompt).strip() or None
    except (AIError, Exception) as exc:  # a missing digest is not a failure
        log.warning("digest for %s failed: %s", month, exc)
        return None


# ---------- top-level -----------------------------------------------------


def run_maintenance(
    cfg: Config,
    *,
    backend: AIBackend | None = None,
    today: date | None = None,
    dry_run: bool = False,
) -> MaintenanceReport:
    report = MaintenanceReport()
    mc = cfg.maintenance
    if not mc.enabled:
        return report

    report.logs_rotated = rotate_logs(
        cfg, max_mb=mc.log_max_mb, keep=mc.log_keep, dry_run=dry_run
    )
    if mc.prune_classification_cache:
        report.cache_entries_pruned = prune_classification_cache(cfg, dry_run=dry_run)

    months, lines, digests = compact_pending_attention(
        cfg,
        keep_days=mc.pending_attention_keep_days,
        today=today,
        backend=backend if mc.use_ai else None,
        dry_run=dry_run,
    )
    report.pending_months_archived = months
    report.pending_lines_archived = lines
    report.digests_written = digests

    if mc.dedup_attachments:
        dedup = attstore.dedup_existing(cfg, dry_run=dry_run)
        report.attachments_linked = dedup.linked
        report.attachment_bytes_saved = dedup.bytes_saved
        if mc.gc_attachment_store:
            files, freed = attstore.gc(cfg, dry_run=dry_run)
            report.store_files_removed = files
            report.store_bytes_freed = freed

    log.info("maintenance done: %s", report.to_dict())
    if mc.notify and report.did_something and not dry_run:
        _send_report(cfg, report)
    return report


def _send_report(cfg: Config, report: MaintenanceReport) -> None:
    from . import notify

    org = cfg.organization.name or "JARLIS"
    body = json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
    notify.send_email(cfg, f"[{org}] monthly maintenance", body)


def is_due_today(cfg: Config, today: date | None = None) -> bool:
    """True when today is the configured day of the month."""
    if not cfg.maintenance.enabled:
        return False
    today = today or date.today()
    return today.day == max(1, min(28, cfg.maintenance.day_of_month))


def _cli_main(argv: list[str] | None = None) -> int:
    import argparse
    import logging as _log
    import sys

    from . import runlock
    from .ai import get_backend
    from .config import load_config

    parser = argparse.ArgumentParser(prog="python -m jarlis.maintenance")
    parser.add_argument("--dry-run", action="store_true", help="report without changing anything")
    parser.add_argument("--force", action="store_true", help="run even if today is not the scheduled day")
    parser.add_argument("--no-ai", action="store_true", help="skip the AI digest step")
    args = parser.parse_args(argv)

    _log.basicConfig(level=_log.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)
    cfg = load_config()

    if not args.force and not is_due_today(cfg):
        log.info("maintenance not due today (day_of_month=%s)", cfg.maintenance.day_of_month)
        return 0

    backend = None
    if cfg.maintenance.use_ai and not args.no_ai and not args.dry_run:
        try:
            backend = get_backend(cfg)
        except Exception as exc:
            log.warning("no AI backend for the digest step: %s", exc)

    with runlock.guard(cfg, "maintenance", timeout_seconds=3600) as decision:
        if not decision.proceed:
            print(json.dumps({"skipped": decision.action}, indent=2))
            return 0
        report = run_maintenance(cfg, backend=backend, dry_run=args.dry_run)
    print(json.dumps(report.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli_main())
