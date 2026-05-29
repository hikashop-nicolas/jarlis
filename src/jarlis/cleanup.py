"""Storage cleanup pass.

Runs weekly (or manually). Three concerns:

  1. **Proactive re-classification**: when the user adds a topic to
     ``ignored_topics.md``, scan ``processed/`` for past emails that now
     match, and move them into ``archived/``.

  2. **Time-based aging**: drop stale entries to ``memory/archive/``:
       * ``memory/people/<email>.md``  if no email activity in ``people_archive_days``
       * ``memory/topics/<slug>.md``   if no matching email in ``topic_archive_days``

  3. **Body retention**: for emails older than
     ``processed_email_keep_days``, delete the body text and raw RFC822
     while keeping ``meta.json`` (audit trail). Set this to ``0`` to keep
     bodies forever.

The ``CleanupReport`` returned by :func:`run_cleanup` is consumed by the
recap to show the user what changed.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from . import classify, memory, pipeline
from .config import Config
from .models import (
    ARCHIVE_IGNORED_TOPIC,
    BUCKET_ARCHIVE,
    Classification,
    Email,
    WhyLogEntry,
)

log = logging.getLogger(__name__)


@dataclass
class CleanupReport:
    re_classified: list[str] = field(default_factory=list)        # folder names moved to archived/
    people_archived: list[str] = field(default_factory=list)      # email-slugs moved
    topics_archived: list[str] = field(default_factory=list)      # slugs moved
    bodies_deleted: list[str] = field(default_factory=list)       # folder names whose body files were stripped

    def to_dict(self) -> dict:
        return {
            "re_classified": self.re_classified,
            "people_archived": self.people_archived,
            "topics_archived": self.topics_archived,
            "bodies_deleted": self.bodies_deleted,
        }


# ---------- 1. proactive re-classification -------------------------------


def _email_matches_ignored(meta: dict, body_text: str, topics: list[memory.IgnoredTopic]) -> tuple[bool, str | None]:
    blob = (
        f"{meta.get('subject', '')}\n"
        f"{meta.get('sender_name', '')}\n"
        f"{meta.get('sender', '')}\n"
        f"{body_text}"
    ).lower()
    for topic in topics:
        if any(kw.lower() in blob for kw in topic.keywords):
            return True, topic.text
    return False, None


def _reclassify_processed(cfg: Config) -> list[str]:
    """Move processed emails matching current ignored_topics into archived/."""
    topics = memory.load_ignored_topics(cfg)
    if not topics:
        return []
    if not cfg.processed_dir.exists():
        return []

    moved: list[str] = []
    for sub in list(cfg.processed_dir.iterdir()):
        if not sub.is_dir():
            continue
        # Only top-level entries (not archived/, spam/).
        if sub.name in ("archived", "spam"):
            continue
        meta_path = sub / "meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        body_path = sub / "body.txt"
        body_text = body_path.read_text(encoding="utf-8", errors="replace") if body_path.exists() else ""
        match, topic_text = _email_matches_ignored(meta, body_text, topics)
        if not match:
            continue

        cls = Classification(
            bucket=BUCKET_ARCHIVE,
            archive_reason=ARCHIVE_IGNORED_TOPIC,
            topic_slugs=[memory.topic_to_slug(topic_text or "")],
            reason=f"cleanup: matched ignored topic {topic_text!r}",
            layer="cleanup",
            confidence=0.95,
        )
        cls.why_log.append(WhyLogEntry.now("cleanup", "re-classified by cleanup pass"))
        meta["classification"] = cls.to_dict()
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        # Reuse the pipeline's helper to move the folder.
        pipeline._move_folder(sub, cfg.archived_dir)
        moved.append(sub.name)
    return moved


# ---------- 2a. age out memory/people/ -----------------------------------


def _latest_activity_per_sender(cfg: Config) -> dict[str, date]:
    """Walk processed + archived to find each sender's most recent email date."""
    out: dict[str, date] = {}
    for root in (cfg.processed_dir, cfg.archived_dir):
        if not root.exists():
            continue
        for meta_path in root.rglob("meta.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            sender = (meta.get("sender") or "").lower()
            if not sender:
                continue
            try:
                d = date.fromisoformat((meta.get("date") or "")[:10])
            except ValueError:
                d = date.fromtimestamp(meta_path.stat().st_mtime)
            prev = out.get(sender)
            if prev is None or d > prev:
                out[sender] = d
    return out


def _age_out_people(cfg: Config, today: date) -> list[str]:
    """Move stale people files into ``memory/archive/people/``."""
    if not cfg.cleanup.enabled or cfg.cleanup.people_archive_days <= 0:
        return []
    activity = _latest_activity_per_sender(cfg)
    cutoff = today - timedelta(days=cfg.cleanup.people_archive_days)
    archived: list[str] = []
    for slug in memory.list_people(cfg):
        # Reverse the slug: alice_at_example_com → alice@example.com.
        sender = _slug_to_email(slug)
        last = activity.get(sender)
        if last is not None and last >= cutoff:
            continue
        # No or stale activity → archive.
        memory.archive_person(cfg, sender)
        archived.append(slug)
    return archived


def _slug_to_email(slug: str) -> str:
    # Inverse of memory.email_to_slug for the most common case.
    s = slug.replace("_at_", "@")
    s = s.replace("_plus_", "+")
    # Replace remaining underscores with dots: alice@example_com → alice@example.com.
    if "@" in s:
        local, _, domain = s.partition("@")
        domain = domain.replace("_", ".")
        s = f"{local}@{domain}"
    return s


# ---------- 2b. age out memory/topics/ -----------------------------------


def _latest_topic_match(cfg: Config) -> dict[str, date]:
    """Walk processed + archived to find the latest match per topic slug."""
    out: dict[str, date] = {}
    for root in (cfg.processed_dir, cfg.archived_dir):
        if not root.exists():
            continue
        for meta_path in root.rglob("meta.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            slugs = (meta.get("classification") or {}).get("topic_slugs") or []
            if not slugs:
                continue
            try:
                d = date.fromisoformat((meta.get("date") or "")[:10])
            except ValueError:
                d = date.fromtimestamp(meta_path.stat().st_mtime)
            for slug in slugs:
                prev = out.get(slug)
                if prev is None or d > prev:
                    out[slug] = d
    return out


def _age_out_topics(cfg: Config, today: date) -> list[str]:
    if not cfg.cleanup.enabled or cfg.cleanup.topic_archive_days <= 0:
        return []
    latest = _latest_topic_match(cfg)
    cutoff = today - timedelta(days=cfg.cleanup.topic_archive_days)
    archived: list[str] = []
    for slug in memory.list_topics(cfg):
        last = latest.get(slug)
        if last is not None and last >= cutoff:
            continue
        memory.archive_topic(cfg, slug)
        archived.append(slug)
    return archived


# ---------- 3. body retention --------------------------------------------


_BODY_FILES = ("body.txt", "body.html", "raw.eml")


def _delete_old_bodies(cfg: Config, today: date) -> list[str]:
    """Strip body / raw files from emails older than ``processed_email_keep_days``."""
    keep_days = cfg.cleanup.processed_email_keep_days
    if not cfg.cleanup.enabled or keep_days <= 0:
        return []
    cutoff = today - timedelta(days=keep_days)
    affected: list[str] = []
    for root in (cfg.processed_dir, cfg.archived_dir):
        if not root.exists():
            continue
        for meta_path in root.rglob("meta.json"):
            folder = meta_path.parent
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            try:
                d = date.fromisoformat((meta.get("date") or "")[:10])
            except ValueError:
                d = date.fromtimestamp(meta_path.stat().st_mtime)
            if d >= cutoff:
                continue

            removed_any = False
            for name in _BODY_FILES:
                p = folder / name
                if p.exists():
                    try:
                        p.unlink()
                        removed_any = True
                    except OSError as exc:
                        log.warning("could not unlink %s: %s", p, exc)
            if removed_any:
                affected.append(folder.name)
                meta["body_deleted_on"] = today.isoformat()
                meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return affected


# ---------- top-level ----------------------------------------------------


def run_cleanup(cfg: Config, *, today: date | None = None) -> CleanupReport:
    today = today or date.today()
    report = CleanupReport()
    if not cfg.cleanup.enabled:
        return report

    report.re_classified = _reclassify_processed(cfg)
    report.people_archived = _age_out_people(cfg, today)
    report.topics_archived = _age_out_topics(cfg, today)
    report.bodies_deleted = _delete_old_bodies(cfg, today)
    log.info("cleanup done: %s", report.to_dict())
    return report


def _cli_main(argv: list[str] | None = None) -> int:
    import argparse
    import logging as _log
    import sys

    from .config import load_config

    parser = argparse.ArgumentParser(prog="python -m jarlis.cleanup")
    parser.add_argument(
        "--print", dest="just_print", action="store_true",
        help="print the report; don't actually move/delete anything (not yet implemented; reserved)",
    )
    parser.parse_args(argv)

    _log.basicConfig(level=_log.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)
    cfg = load_config()
    report = run_cleanup(cfg)
    print(json.dumps(report.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_cli_main())
