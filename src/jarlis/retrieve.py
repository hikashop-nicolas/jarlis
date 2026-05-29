"""Structured retrieval of context for draft generation.

When drafting a reply, JARLIS gathers:

  - Same-sender history: last N emails from the same correspondent
  - Thread walk: all messages with the same In-Reply-To / References chain
  - Topic-tag matches: topic files whose keywords match the current email
  - Voice exemplars: representative sent emails in the relevant language(s)

The result is a single string that the pipeline injects as a
"## Past similar exchanges" block in the draft prompt.

This module deliberately uses NO embeddings: all retrieval is keyword /
header / filename based. Embedding-based semantic retrieval is deferred
to v1.5 (see PLAN §10).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from . import memory
from .config import Config
from .models import Email

log = logging.getLogger(__name__)

DEFAULT_SENDER_HISTORY = 5
DEFAULT_TOTAL_BUDGET = 12  # max number of past messages we'll inject


@dataclass
class RetrievedMessage:
    """One past message picked up by the retrieval pass."""

    source: str           # "same-sender" | "thread" | "topic"
    sender: str
    subject: str
    date: str             # ISO-8601 string
    snippet: str          # first ~500 chars of body
    path: Path | None = None


def retrieve_context(
    cfg: Config,
    email: Email,
    *,
    history_root: Path | None = None,
    sender_n: int = DEFAULT_SENDER_HISTORY,
    total_budget: int = DEFAULT_TOTAL_BUDGET,
) -> list[RetrievedMessage]:
    """Build the list of past messages relevant to ``email``.

    ``history_root`` is the ``processed/`` directory: typically
    ``cfg.processed_dir``. Each subfolder is one processed email
    (``meta.json`` + ``body.txt``).
    """
    if history_root is None:
        history_root = cfg.processed_dir

    selected: list[RetrievedMessage] = []
    seen_paths: set[Path] = set()

    # 1) Same-sender history (most recent N).
    for msg in _last_from_sender(history_root, email.sender, n=sender_n):
        if msg.path in seen_paths:
            continue
        seen_paths.add(msg.path) if msg.path else None
        selected.append(msg)

    # 2) Thread walk via In-Reply-To / References.
    thread_ids = set(email.references or [])
    if email.in_reply_to:
        thread_ids.add(email.in_reply_to)
    if thread_ids:
        for msg in _thread_members(history_root, thread_ids):
            if msg.path in seen_paths:
                continue
            seen_paths.add(msg.path) if msg.path else None
            selected.append(msg)

    # 3) Topic-tag matches: pick a few examples from each matched topic.
    matched = _topics_matching_subject(cfg, email.subject + "\n" + email.body_text)
    if matched and len(selected) < total_budget:
        budget_left = total_budget - len(selected)
        for msg in _topic_examples(history_root, matched, n=budget_left):
            if msg.path in seen_paths:
                continue
            seen_paths.add(msg.path) if msg.path else None
            selected.append(msg)
            if len(selected) >= total_budget:
                break

    return selected[:total_budget]


def render_for_prompt(messages: list[RetrievedMessage]) -> str:
    """Format retrieved messages as a markdown block for prompt injection."""
    if not messages:
        return ""
    lines = ["## Past similar exchanges", ""]
    for m in messages:
        lines.append(f"### [{m.source}] {m.date}: {m.sender}")
        lines.append(f"Subject: {m.subject}")
        lines.append("")
        lines.append(m.snippet.strip())
        lines.append("")
    return "\n".join(lines)


# ---------- on-disk scanning ---------------------------------------------


def _iter_meta(history_root: Path) -> Iterable[tuple[Path, dict]]:
    """Yield (folder, meta_dict) for every processed email under ``history_root``."""
    if not history_root.exists():
        return
    for meta_path in history_root.rglob("meta.json"):
        try:
            yield meta_path.parent, json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log.warning("skipping unreadable meta.json %s: %s", meta_path, e)


def _read_body_snippet(folder: Path, n: int = 500) -> str:
    body = folder / "body.txt"
    if not body.exists():
        return ""
    try:
        return body.read_text(encoding="utf-8", errors="replace")[:n]
    except OSError:
        return ""


def _to_message(folder: Path, meta: dict, source: str) -> RetrievedMessage:
    return RetrievedMessage(
        source=source,
        sender=meta.get("sender", ""),
        subject=meta.get("subject", ""),
        date=meta.get("date", ""),
        snippet=_read_body_snippet(folder),
        path=folder,
    )


def _last_from_sender(history_root: Path, sender: str, n: int) -> list[RetrievedMessage]:
    if not sender:
        return []
    sender_lc = sender.lower()
    matches: list[tuple[str, Path, dict]] = []
    for folder, meta in _iter_meta(history_root):
        if (meta.get("sender") or "").lower() == sender_lc:
            matches.append((meta.get("date", ""), folder, meta))
    matches.sort(key=lambda t: t[0], reverse=True)
    return [_to_message(f, m, "same-sender") for _, f, m in matches[:n]]


def _thread_members(history_root: Path, thread_ids: set[str]) -> list[RetrievedMessage]:
    if not thread_ids:
        return []
    out: list[RetrievedMessage] = []
    for folder, meta in _iter_meta(history_root):
        mid = meta.get("message_id") or ""
        refs = set(meta.get("references") or [])
        in_reply_to = meta.get("in_reply_to") or ""
        if mid in thread_ids or in_reply_to in thread_ids or refs & thread_ids:
            out.append(_to_message(folder, meta, "thread"))
    out.sort(key=lambda m: m.date)
    return out


def _topics_matching_subject(cfg: Config, blob: str) -> list[str]:
    """Return topic slugs whose `**Subject keywords**: ...` line hits ``blob``."""
    blob_lc = blob.lower()
    out: list[str] = []
    for slug in memory.list_topics(cfg):
        body = memory.load_topic(cfg, slug) or ""
        for line in body.splitlines():
            stripped = line.strip().lower()
            if "subject keyword" not in stripped:
                continue
            _, _, kws = stripped.partition(":")
            for k in kws.split(","):
                k = k.strip().strip("`'\"")
                if k and k in blob_lc:
                    out.append(slug)
                    break
            break
    return out


def _topic_examples(history_root: Path, topic_slugs: list[str], n: int) -> list[RetrievedMessage]:
    if not topic_slugs or n <= 0:
        return []
    wanted = set(topic_slugs)
    matches: list[tuple[str, Path, dict]] = []
    for folder, meta in _iter_meta(history_root):
        slugs = set(meta.get("classification", {}).get("topic_slugs") or [])
        if slugs & wanted:
            matches.append((meta.get("date", ""), folder, meta))
    matches.sort(key=lambda t: t[0], reverse=True)
    return [_to_message(f, m, "topic") for _, f, m in matches[:n]]
