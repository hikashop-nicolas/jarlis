"""Email body cleanup utilities.

Strips boilerplate that bloats every email's body without adding signal:

  - Quoted previous emails (lines starting with ``>``)
  - Everything after a separator marker (``***``, ``---``, ``___``, ``===``)
  - Quote-introducer lines (``On <date>, <person> wrote:``,
    ``Le <date> à <time>, <person> a écrit :``, etc.)
  - Domain-specific footers learned at bootstrap time

Used at three points in the pipeline:

  - Notification display (the user sees a clean email body)
  - Translation input (don't translate quoted history)
  - Draft prompt input (the AI sees the relevant content, not boilerplate)

The original ``body_text`` on disk is never modified — cleaning is on demand.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path

# Lines composed of repeated separator characters. Length ≥ 10 keeps short
# bullet-list dashes from being mis-detected.
_SEPARATOR_RE = re.compile(r"^\s*([*_\-=#~+]){10,}\s*$")

# Japanese Gmail's quote-intro line has no "wrote:" verb; it's just a
# date stamp + display name + ``<email>:``. Example:
#   ``2026年5月9日(土) 9:06 Hanako Sato <hanako.sato@example.com>:``
# We match the ``YYYY年M月D日`` prefix followed eventually by ``<…@…>:``.
_JP_DATE_QUOTE_RE = re.compile(
    r"^\s*\d{4}年\d{1,2}月\d{1,2}日.*<[^@>\s]+@[^>\s]+>\s*[:：]\s*$"
)

# Quote-introducer lines we cut at. Pattern is "<verb-leading-prefix> ... <wrote-phrase> ...".
# Conservative on purpose: only match obvious header lines.
_QUOTE_INTROS = (
    "wrote:",            # English Gmail / generic
    "wrote :",           # English with extra space
    "a écrit :",         # French Gmail
    "a écrit:",          # French Gmail no space
    "schrieb:",          # German
    "schreef:",          # Dutch
    "ha scritto:",       # Italian
    "escribió:",         # Spanish
    "が書き込みました",  # Japanese (rare in email)
)

# Phone / mobile sign-offs that are footers but not separator-marked.
_TRAILER_PHRASES = (
    "sent from my iphone",
    "sent from my android",
    "sent from my mobile",
    "envoyé de mon iphone",
    "envoyé depuis mon mobile",
    "get outlook for ios",
    "get outlook for android",
)


def _is_separator(line: str) -> bool:
    return bool(_SEPARATOR_RE.match(line))


def _is_quote_intro(line: str) -> bool:
    if _JP_DATE_QUOTE_RE.match(line):
        return True
    s = line.strip().lower()
    if not s:
        return False
    return any(p in s for p in _QUOTE_INTROS)


def _is_trailer_phrase(line: str) -> bool:
    s = line.strip().lower()
    return s in _TRAILER_PHRASES


def clean_body(
    body: str,
    *,
    domain_footer: str | None = None,
) -> str:
    """Return ``body`` with quoted history, separator-trailers, and (optionally)
    a known domain footer stripped.

    ``domain_footer`` is a per-domain footer string previously learned at
    bootstrap time. If ``body`` ends with it (after some whitespace
    normalization), it's removed; otherwise the value is ignored.
    """
    if not body:
        return ""

    text = body
    # Remove a known domain footer first if it's a clean trailing match.
    if domain_footer:
        stripped = text.rstrip()
        df_stripped = domain_footer.rstrip()
        if df_stripped and stripped.endswith(df_stripped):
            text = stripped[: -len(df_stripped)].rstrip()

    out: list[str] = []
    for line in text.splitlines():
        if _is_separator(line) or _is_quote_intro(line) or _is_trailer_phrase(line):
            break
        if line.lstrip().startswith(">"):
            continue
        out.append(line)

    return "\n".join(out).rstrip()


# ---------- per-domain footer learning -----------------------------------


def _longest_common_suffix(strings: list[str]) -> str:
    """Longest string that is a suffix of every element of ``strings``.

    Uses character-by-character reverse comparison; O(n × min_len).
    Returns ``""`` if any string is empty or the inputs disagree from
    the start. Kept for tests and possible reuse; ``autodetect_footers``
    no longer relies on this because real-world emails rarely share a
    full trailing suffix once quoted threads vary.
    """
    if not strings:
        return ""
    if any(not s for s in strings):
        return ""
    shortest_len = min(len(s) for s in strings)
    matched = 0
    for i in range(1, shortest_len + 1):
        char = strings[0][-i]
        if all(s[-i] == char for s in strings):
            matched = i
        else:
            break
    return strings[0][-matched:] if matched else ""


def extract_candidate_footer(body: str) -> str:
    """Pull the candidate signature-footer block out of a single email body.

    Heuristic, in order:

      1. Cut at the first quote-introducer (``On ... wrote:``,
         ``Le ... a écrit :``, etc.) so we look at *fresh* content only.
      2. Drop ``>``-prefixed quoted lines from the fresh portion.
      3. Find the FIRST separator line (``***``, ``---``, ``===``, etc.)
         in that fresh content. Everything from after that separator to
         the end of the fresh portion is the candidate footer (the
         organization's address/contact block lives there).

    Returns ``""`` when no suitable separator is found.
    """
    if not body:
        return ""
    lines = body.splitlines()
    fresh_end = len(lines)
    for i, line in enumerate(lines):
        if _is_quote_intro(line):
            fresh_end = i
            break
    fresh = [ln for ln in lines[:fresh_end] if not ln.lstrip().startswith(">")]
    first_sep = -1
    for i, line in enumerate(fresh):
        if _is_separator(line):
            first_sep = i
            break
    if first_sep == -1:
        return ""
    after_lines = fresh[first_sep + 1:]
    return "\n".join(after_lines).strip("\n")


def autodetect_footers(
    emails_with_domain: Iterable[tuple[str, str]],
    *,
    min_emails: int = 3,
    min_chars: int = 80,
    min_lines: int = 2,
) -> dict[str, str]:
    """Learn per-domain footers from a corpus.

    ``emails_with_domain`` yields ``(domain, body)`` pairs. We:

      1. Run :func:`extract_candidate_footer` on each body to pull the
         "after the first separator" block out of fresh content.
      2. Group candidates by domain.
      3. Pick the most common exact-match candidate per domain (real
         signatures repeat verbatim across emails from the same sender
         org).
      4. Accept the candidate when seen at least ``min_emails`` times
         and substantial enough (``min_chars``, ``min_lines``).

    Returns ``{domain: footer_text}``.
    """
    from collections import Counter, defaultdict

    by_domain: dict[str, list[str]] = defaultdict(list)
    for domain, body in emails_with_domain:
        if domain and body:
            cand = extract_candidate_footer(body)
            if cand:
                by_domain[domain].append(cand)

    out: dict[str, str] = {}
    for domain, candidates in by_domain.items():
        if len(candidates) < min_emails:
            continue
        counts = Counter(candidates)
        best, count = counts.most_common(1)[0]
        if (
            count >= min_emails
            and len(best) >= min_chars
            and best.count("\n") >= min_lines
        ):
            out[domain] = best
    return out


# ---------- persistence -------------------------------------------------


def save_footers(path: Path, footers: dict[str, str]) -> None:
    """Persist learned footers to JSON. ``path.parent`` is created if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(footers, ensure_ascii=False, indent=2), encoding="utf-8")


def load_footers(path: Path) -> dict[str, str]:
    """Load footers JSON; returns ``{}`` on missing or unreadable file."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items() if v}
    except (json.JSONDecodeError, OSError):
        return {}


def domain_of(sender: str) -> str:
    """Return the lowercased domain part of an email address; ``""`` if absent."""
    if not sender or "@" not in sender:
        return ""
    return sender.rsplit("@", 1)[1].strip().lower()
