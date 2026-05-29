"""
Cross-platform safe folder/file name generation.

Produces names that work on FAT32 / NTFS / exFAT / ext4 / APFS without issues,
even when the source string contains CJK characters, full-width punctuation,
emoji, etc. This matters for:
  - Migrating the project to a USB key (often FAT32) or older Windows
  - Cross-mounting between OS
  - Any future archiving / zipping that expects portable names

Strategy:
  1. NFKC-normalize the input: converts full-width punctuation to ASCII
     (e.g. 【】→[] / 　→space / ～→~ / （）→() / fullwidth digits→ASCII digits).
  2. Replace whitespace, forbidden filesystem chars, and remaining CJK
     punctuation with underscores.
  3. By default, strip everything that isn't ASCII alphanumeric / `_` / `-` / `.`.
  4. Append a 6-char content hash so two distinct sources with identical
     truncations stay distinct (and so the same source always produces the
     same name).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

_FORBIDDEN_FS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_CJK_PUNCT = re.compile(r"[「」『』、。〜〝〞]")
_WHITESPACE = re.compile(r"\s+")
_MULTI_UNDERSCORE = re.compile(r"_+")
_ASCII_KEEP = re.compile(r"[A-Za-z0-9_\-.]")


def _content_hash(raw: str) -> str:
    if not raw:
        return "noid"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:6]


def make_safe_filename(name: str, max_len: int = 80) -> str:
    """Generate an ASCII-safe filename, preserving the extension.

    Args:
        name:    arbitrary filename (e.g. "【別添】資料 (令和7年).xlsx").
        max_len: total max length of the result, extension included.

    Returns:
        Safe filename of the form `<readable>_<hash>.<ext>` or `file_<hash>.<ext>`.
        Always ASCII; the extension is preserved (lowercased, ASCII only).
    """
    name = (name or "").strip()
    if not name:
        return "unnamed"

    if "." in name and not name.startswith("."):
        stem, _, ext = name.rpartition(".")
    else:
        stem, ext = name, ""

    ext_clean = "".join(c for c in unicodedata.normalize("NFKC", ext) if _ASCII_KEEP.match(c))
    ext_part = f".{ext_clean.lower()}" if ext_clean else ""

    h = _content_hash(stem)
    text = unicodedata.normalize("NFKC", stem)
    text = _WHITESPACE.sub("_", text)
    text = _FORBIDDEN_FS_CHARS.sub("_", text)
    text = _CJK_PUNCT.sub("_", text)
    ascii_text = "".join(c for c in text if _ASCII_KEEP.match(c))
    ascii_text = _MULTI_UNDERSCORE.sub("_", ascii_text).strip("_.")

    suffix = f"_{h}{ext_part}"
    available = max_len - len(suffix)
    readable = ascii_text[:available].strip("_") if (available > 0 and ascii_text) else ""

    if readable:
        return f"{readable}{suffix}"
    return f"file{suffix}"


def make_safe_name(prefix: str, raw: str, max_len: int = 80) -> str:
    """Generate an ASCII-safe folder/file name.

    Args:
        prefix:  ASCII prefix kept verbatim (e.g. "20260507"). Caller's
                 responsibility to ensure it is itself safe.
        raw:     arbitrary text (an email subject, a description...).
        max_len: total max length of the returned name in chars.

    Returns:
        A name of the form `<prefix>_<readable>_<hash>` or `<prefix>_<hash>`
        when no ASCII-readable content survives normalization. Always ASCII,
        always <= max_len chars.
    """
    raw = (raw or "").strip()
    h = _content_hash(raw)

    text = unicodedata.normalize("NFKC", raw)
    text = _WHITESPACE.sub("_", text)
    text = _FORBIDDEN_FS_CHARS.sub("_", text)
    text = _CJK_PUNCT.sub("_", text)

    ascii_text = "".join(c for c in text if _ASCII_KEEP.match(c))
    ascii_text = _MULTI_UNDERSCORE.sub("_", ascii_text).strip("_")

    suffix = f"_{h}"
    sep_len = 1 if (prefix and ascii_text) else 0
    available = max_len - len(prefix) - sep_len - len(suffix)

    readable = ascii_text[:available].strip("_") if (available > 0 and ascii_text) else ""

    parts = [p for p in (prefix, readable) if p]
    return "_".join(parts) + suffix
