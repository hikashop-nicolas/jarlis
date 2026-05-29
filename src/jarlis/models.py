"""Common data models passed between JARLIS modules.

Kept lightweight on purpose: the classifier, retriever, and pipeline all
agree on these shapes. The on-disk representation (``meta.json`` for
processed emails, etc.) is a serialization of these dataclasses.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

# Three triage buckets (see PLAN §8).
BUCKET_DRAFTED = "drafted"
BUCKET_FLAGGED = "flagged"
BUCKET_ARCHIVE = "archive"
BUCKETS: tuple[str, ...] = (BUCKET_DRAFTED, BUCKET_FLAGGED, BUCKET_ARCHIVE)

# Sub-flags for archive bucket.
ARCHIVE_IGNORED_TOPIC = "ignored_topic"
ARCHIVE_SPAM = "spam"
ARCHIVE_LOW_PRIORITY = "low_priority"
ARCHIVE_RESOLVED = "resolved"
ARCHIVE_NOT_ADDRESSED = "not_addressed"
ARCHIVE_REASONS: tuple[str, ...] = (
    ARCHIVE_IGNORED_TOPIC,
    ARCHIVE_SPAM,
    ARCHIVE_LOW_PRIORITY,
    ARCHIVE_RESOLVED,
    ARCHIVE_NOT_ADDRESSED,
)

# Classifier layers (recorded in ``Classification.layer``).
LAYER_CACHE = "cache"
LAYER_RULES = "rules"
LAYER_LLM = "llm"


@dataclass
class Email:
    """Minimal in-memory representation of an email being processed."""

    message_id: str
    sender: str                       # bare email address (lower-cased)
    sender_name: str = ""             # display name; empty if not provided
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    subject: str = ""
    date: datetime | None = None
    body_text: str = ""
    body_html: str | None = None
    attachments: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    in_reply_to: str = ""
    references: list[str] = field(default_factory=list)


@dataclass
class WhyLogEntry:
    """One line of the per-email "why" log."""

    layer: str            # LAYER_CACHE / LAYER_RULES / LAYER_LLM / "manual"
    decision: str         # short phrase, e.g. "matched ignored topic 'lunch'"
    timestamp: str = ""   # ISO-8601 string

    @classmethod
    def now(cls, layer: str, decision: str) -> "WhyLogEntry":
        return cls(layer=layer, decision=decision, timestamp=datetime.now().isoformat(timespec="seconds"))


@dataclass
class Classification:
    """The result of classifying one email."""

    bucket: str = BUCKET_FLAGGED                  # default: route to flagged if classifier is uncertain
    archive_reason: str | None = None             # only meaningful when bucket == BUCKET_ARCHIVE
    topic_slugs: list[str] = field(default_factory=list)
    reason: str = ""                              # one-liner shown in the recap
    layer: str = LAYER_LLM                        # which layer decided
    confidence: float = 1.0                       # 0..1; informational only
    why_log: list[WhyLogEntry] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Classification":
        why = [WhyLogEntry(**e) for e in d.get("why_log", [])]
        d = {**d, "why_log": why}
        return cls(**d)

    def write_meta(self, path: Path) -> None:
        """Persist this classification to ``path`` as JSON (UTF-8)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
