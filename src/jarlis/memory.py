"""Memory tree management.

The memory tree is a structured set of markdown files describing the user,
their organization, recurring contacts, recurring topics, and writing style.
JARLIS reads it before each AI call; bootstrap and auto-curators write to it.

Layout (relative to ``cfg.memory_dir``):

    00_organization.md                  : org identity, public URL, language conventions
    me.md                               : the user's profile (role, name variants, languages)
    preferences.md                      : email writing style: tone, signature, formatting rules
    ignored_topics.md                   : user-curated list of topics to silence
    people/<email-slug>.md              : one file per known correspondent
    topics/<slug>.md                    : recurring subjects (subject keywords + senders)
    voice/<lang>/exemplar_NN.md         : representative sent emails for drafting tone
    archive/people/<email-slug>.md      : aged-out contacts (cleanup target)
    archive/topics/<slug>.md            : aged-out topics

All files are plain UTF-8 markdown. Users can edit anything by hand at any time.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)

# Top-level files (no subdirectory). Each maps to a single markdown file.
TOP_LEVEL_SECTIONS: tuple[str, ...] = (
    "00_organization",
    "me",
    "preferences",
    "ignored_topics",
)


@dataclass
class IgnoredTopic:
    """A single entry from ``ignored_topics.md``.

    The text is the human-readable description; ``keywords`` are the
    substrings extracted for matching against subject + body.
    """

    text: str
    keywords: list[str]


# ---------- path helpers --------------------------------------------------


def section_path(cfg: Config, section: str) -> Path:
    """Return the path to a top-level memory file."""
    if section not in TOP_LEVEL_SECTIONS:
        raise ValueError(f"unknown top-level section: {section!r}")
    return cfg.memory_dir / f"{section}.md"


def auto_footers_path(cfg: Config) -> Path:
    """Path to the persisted per-domain auto-detected footers JSON.

    Bootstrap writes this; runtime cleanup reads it to strip recurring
    boilerplate before the AI sees the email body. Editable; the user can
    delete entries that don't apply.
    """
    return cfg.memory_dir / "auto_footers.json"


def shared_addresses_path(cfg: Config) -> Path:
    """Path to the persisted auto-detected shared-addresses file.

    One address per line. Written by ``bootstrap`` when it detects
    addresses with multiple in-body signers; read by the runtime
    classifier and pipeline so they can route per-person memory
    correctly without re-scanning history each time.
    """
    return cfg.memory_dir / "auto_shared_addresses.txt"


def load_shared_addresses(cfg: Config) -> set[str]:
    """Combined shared-address set: config + auto-detected file."""
    out = {a.strip().lower() for a in cfg.pipeline.shared_addresses if a and a.strip()}
    p = shared_addresses_path(cfg)
    if p.exists():
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip().lower()
                if line and not line.startswith("#"):
                    out.add(line)
        except OSError:
            pass
    return out


def save_shared_addresses(cfg: Config, addresses: set[str]) -> Path:
    """Write the auto-detected set to ``memory/auto_shared_addresses.txt``.

    Header documents the file's purpose so anyone editing it knows what
    they're touching.
    """
    p = shared_addresses_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Auto-detected shared mailbox addresses",
        "#",
        "# Each line is an email address where JARLIS observed multiple distinct",
        "# people signing emails via the '<name>より … <name>' convention. Mail",
        "# from these addresses is attributed to individuals via in-body name",
        "# instead of the From: header, so each person gets their own memory file.",
        "#",
        "# Edit freely. Lines beginning with '#' are comments.",
        "",
    ]
    lines.extend(sorted(addresses))
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def people_dir(cfg: Config) -> Path:
    return cfg.memory_dir / "people"


def topics_dir(cfg: Config) -> Path:
    return cfg.memory_dir / "topics"


def voice_dir(cfg: Config, lang: str | None = None) -> Path:
    base = cfg.memory_dir / "voice"
    return base / lang if lang else base


def archive_dir(cfg: Config, kind: str) -> Path:
    if kind not in ("people", "topics"):
        raise ValueError(f"unknown archive kind: {kind!r}")
    return cfg.memory_dir / "archive" / kind


def email_to_slug(email: str) -> str:
    """Convert an email address to a deterministic safe filename slug.

    Most emails are ASCII so we keep things readable: lowercase the local
    part and domain, replace ``@`` with ``_at_`` and any non-[a-z0-9_-] with
    underscore. Examples:
        Sato@acme.example   -> sato_at_acme_example
        user+tag@gmail.com  -> user_plus_tag_at_gmail_com
    """
    s = (email or "").strip().lower()
    s = s.replace("@", "_at_").replace("+", "_plus_")
    s = re.sub(r"[^a-z0-9_\-]", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "unknown"


def name_to_slug(name: str) -> str:
    """Slug for a person's name (used when the address is shared / unknown).

    Latin names: lowercase + underscore-join (``Alice Smith`` -> ``alice_smith``).
    Non-Latin names (CJK / Cyrillic / Arabic / etc.): we can't transliterate
    without a library, so we use a stable 8-char hash prefix with ``person_``
    so the filename is filesystem-safe. The original name is preserved
    inside the memory file's content (header line).
    """
    s = (name or "").strip()
    if not s:
        return "unknown"
    import unicodedata
    nfkd = unicodedata.normalize("NFKD", s)
    ascii_only = "".join(
        c.lower() if c.isalpha() else c
        for c in nfkd if c.isascii() and (c.isalnum() or c in " _-")
    )
    cleaned = re.sub(r"\s+", "_", ascii_only).strip("_-")
    cleaned = re.sub(r"_+", "_", cleaned)
    if cleaned and any(c.isalnum() for c in cleaned):
        return cleaned
    import hashlib
    h = hashlib.md5(s.encode("utf-8")).hexdigest()[:8]
    return f"person_{h}"


def person_to_slug(key: str) -> str:
    """Smart slugifier for a person key.

    If ``key`` looks like an email (has ``@``), uses :func:`email_to_slug`.
    Otherwise treats it as a name and uses :func:`name_to_slug`.
    """
    if "@" in (key or ""):
        return email_to_slug(key)
    return name_to_slug(key)


def topic_to_slug(topic: str) -> str:
    """Lowercase, ASCII-only slug for a topic name."""
    s = (topic or "").strip().lower()
    s = re.sub(r"[^a-z0-9_\-]", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "untitled"


# ---------- layout --------------------------------------------------------


def ensure_layout(cfg: Config) -> None:
    """Create all required subdirectories. Idempotent; safe to call repeatedly."""
    cfg.memory_dir.mkdir(parents=True, exist_ok=True)
    people_dir(cfg).mkdir(parents=True, exist_ok=True)
    topics_dir(cfg).mkdir(parents=True, exist_ok=True)
    voice_dir(cfg).mkdir(parents=True, exist_ok=True)
    (cfg.memory_dir / "archive" / "people").mkdir(parents=True, exist_ok=True)
    (cfg.memory_dir / "archive" / "topics").mkdir(parents=True, exist_ok=True)


# ---------- top-level sections -------------------------------------------


def load_section(cfg: Config, section: str) -> str | None:
    """Return the content of a top-level section file or None if absent."""
    path = section_path(cfg, section)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def save_section(cfg: Config, section: str, content: str) -> Path:
    """Write a top-level section file. Creates parent dirs as needed."""
    path = section_path(cfg, section)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ---------- people --------------------------------------------------------


def list_people(cfg: Config) -> list[str]:
    """Return slugs (filenames without ``.md``) of all known contacts."""
    d = people_dir(cfg)
    if not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.md"))


def load_person(cfg: Config, email: str) -> str | None:
    """Return the memory file content for ``email`` or None if absent."""
    path = people_dir(cfg) / f"{person_to_slug(email)}.md"
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def save_person(cfg: Config, key: str, content: str) -> Path:
    """Write ``memory/people/<slug>.md`` for ``key`` (email or name)."""
    d = people_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{person_to_slug(key)}.md"
    path.write_text(content, encoding="utf-8")
    return path


def archive_person(cfg: Config, email: str) -> Path | None:
    """Move ``people/<slug>.md`` into ``archive/people/``. Returns new path."""
    return archive_person_slug(cfg, person_to_slug(email))


def archive_person_slug(cfg: Config, slug: str) -> Path | None:
    """Archive by slug, for callers that already hold the filename stem.

    Slugging is lossy (``a.b@c.com`` and ``a_b@c.com`` collide), so a caller
    holding a slug must not try to rebuild the address to call
    :func:`archive_person`: it would rebuild a different address.
    """
    src = people_dir(cfg) / f"{slug}.md"
    if not src.exists():
        return None
    dst_dir = archive_dir(cfg, "people")
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    src.replace(dst)
    return dst


def restore_person(cfg: Config, email: str) -> Path | None:
    """Move an archived contact back to the active people directory."""
    src = archive_dir(cfg, "people") / f"{person_to_slug(email)}.md"
    if not src.exists():
        return None
    d = people_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    dst = d / src.name
    src.replace(dst)
    return dst


# ---------- topics --------------------------------------------------------


def list_topics(cfg: Config) -> list[str]:
    d = topics_dir(cfg)
    if not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.md"))


def load_topic(cfg: Config, slug: str) -> str | None:
    path = topics_dir(cfg) / f"{topic_to_slug(slug)}.md"
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def save_topic(cfg: Config, slug: str, content: str) -> Path:
    d = topics_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{topic_to_slug(slug)}.md"
    path.write_text(content, encoding="utf-8")
    return path


def archive_topic(cfg: Config, slug: str) -> Path | None:
    src = topics_dir(cfg) / f"{topic_to_slug(slug)}.md"
    if not src.exists():
        return None
    dst_dir = archive_dir(cfg, "topics")
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    src.replace(dst)
    return dst


# ---------- voice exemplars ----------------------------------------------


def list_voice_exemplars(cfg: Config, lang: str) -> list[tuple[str, str]]:
    """Return ``(name, content)`` tuples for every exemplar in ``lang``."""
    d = voice_dir(cfg, lang)
    if not d.exists():
        return []
    out: list[tuple[str, str]] = []
    for p in sorted(d.glob("exemplar_*.md")):
        out.append((p.stem, p.read_text(encoding="utf-8")))
    return out


def save_voice_exemplar(cfg: Config, lang: str, index: int, content: str) -> Path:
    d = voice_dir(cfg, lang)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"exemplar_{index:02d}.md"
    path.write_text(content, encoding="utf-8")
    return path


# ---------- ignored topics parsing ---------------------------------------

_BULLET_RE = re.compile(r"^\s*[-*]\s+(.+?)\s*$")


def load_ignored_topics(cfg: Config) -> list[IgnoredTopic]:
    """Parse ``ignored_topics.md``: each ``- foo`` bullet becomes one entry.

    Optional inline keywords syntax: ``- description (keywords: foo, bar)``.
    If no keywords block is present, the description itself is the keyword.
    """
    raw = load_section(cfg, "ignored_topics")
    if not raw:
        return []

    out: list[IgnoredTopic] = []
    for line in raw.splitlines():
        m = _BULLET_RE.match(line)
        if not m:
            continue
        text = m.group(1).strip()
        keywords: list[str] = []
        kw_match = re.search(r"\(keywords?:\s*(.+?)\)\s*$", text, re.IGNORECASE)
        if kw_match:
            text = text[: kw_match.start()].strip()
            keywords = [k.strip() for k in kw_match.group(1).split(",") if k.strip()]
        if not keywords:
            keywords = [text]
        out.append(IgnoredTopic(text=text, keywords=keywords))
    return out


# ---------- prompt concatenation -----------------------------------------


def render_for_prompt(
    cfg: Config,
    *,
    sender_email: str | None = None,
    languages: list[str] | None = None,
    topic_slugs: list[str] | None = None,
    include_voice: bool = True,
) -> str:
    """Build the memory block to inject into an AI prompt.

    Includes:
      - organization, me, preferences (if present)
      - the sender's people file (if known)
      - any topic files in ``topic_slugs``
      - voice exemplars for each language in ``languages`` (if include_voice)

    Sections are joined by ``\\n\\n---\\n\\n`` separators.
    """
    parts: list[str] = []

    for section in ("00_organization", "me", "preferences"):
        body = load_section(cfg, section)
        if body:
            parts.append(body.strip())

    if sender_email:
        person = load_person(cfg, sender_email)
        if person:
            parts.append(f"## Contact: {sender_email}\n\n{person.strip()}")

    for slug in topic_slugs or []:
        body = load_topic(cfg, slug)
        if body:
            parts.append(f"## Topic: {slug}\n\n{body.strip()}")

    if include_voice:
        for lang in languages or []:
            for name, body in list_voice_exemplars(cfg, lang):
                parts.append(f"## Voice exemplar ({lang}): {name}\n\n{body.strip()}")

    return "\n\n---\n\n".join(parts)
