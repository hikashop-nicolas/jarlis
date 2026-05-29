"""Attachment text extraction.

If an email arrives with attachments, the draft is usually about the
attachment ("please review the attached form", "see budget below").
Without extracting the attachment text, the AI is replying blind.

Supported formats:

  - ``.pdf``       (via pypdf)
  - ``.docx``      (via python-docx)
  - ``.txt`` / ``.md`` / plain text (read directly)
  - everything else: extraction is skipped; the file path still goes
    into the notification so the user can open it manually.

For each extracted attachment, JARLIS writes a sibling
``<filename>.extracted.txt`` next to the original. This lets the user
open the extracted text directly from the notification email and lets
the pipeline reuse the extraction across runs without re-parsing.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

log = logging.getLogger(__name__)

# Per-attachment cap on extracted characters (extractor output, before
# truncation for AI prompts or notifications). Some PDFs are huge.
MAX_EXTRACTED_CHARS = 50_000

# Short preview included inline in the AI prompt, just enough to give
# the model context. The full path to the extracted-text sibling is also
# in the prompt so backends with file-read capability (Claude with
# --allowedTools Read, Codex with read-only sandbox) can fetch the
# full content if they need to.
PROMPT_PREVIEW_CHARS = 800

# What we'll show in the notification email per attachment.
NOTIFICATION_PER_ATTACHMENT_CHARS = 1500

EXTRACTED_SUFFIX = ".extracted.txt"

PLAIN_TEXT_EXTENSIONS = (".txt", ".md", ".log", ".csv", ".tsv", ".json", ".xml", ".html", ".htm")
PDF_EXTENSIONS = (".pdf",)
DOCX_EXTENSIONS = (".docx",)


def can_extract(filename: str) -> bool:
    """True if JARLIS knows how to pull text out of ``filename``."""
    lower = filename.lower()
    return (
        lower.endswith(PLAIN_TEXT_EXTENSIONS)
        or lower.endswith(PDF_EXTENSIONS)
        or lower.endswith(DOCX_EXTENSIONS)
    )


def extract_text(path: Path) -> str | None:
    """Return extracted text for ``path``, or ``None`` if extraction failed.

    Empty results (parser worked but file was blank/image-only) return ``""``
    so callers can distinguish "we tried and got nothing" from "we couldn't
    even try".
    """
    name = path.name.lower()
    try:
        if name.endswith(PLAIN_TEXT_EXTENSIONS):
            return _read_plaintext(path)
        if name.endswith(PDF_EXTENSIONS):
            return _read_pdf(path)
        if name.endswith(DOCX_EXTENSIONS):
            return _read_docx(path)
    except Exception as exc:
        log.warning("attachment extraction failed for %s: %s", path, exc)
        return None
    return None


def extract_and_save(att_path: Path) -> Path | None:
    """Extract text for ``att_path`` and write a sibling ``.extracted.txt``.

    Returns the path to the .extracted.txt file on success, or None if
    extraction was skipped or failed.
    """
    if not att_path.exists():
        return None
    if not can_extract(att_path.name):
        return None
    text = extract_text(att_path)
    if text is None:
        return None
    capped = text[:MAX_EXTRACTED_CHARS]
    if len(text) > MAX_EXTRACTED_CHARS:
        capped += "\n[... truncated by JARLIS ...]"
    out = att_path.with_name(att_path.name + EXTRACTED_SUFFIX)
    out.write_text(capped, encoding="utf-8")
    return out


def load_extracted(att_path: Path) -> str | None:
    """Load the previously-saved ``.extracted.txt`` for ``att_path``."""
    sibling = att_path.with_name(att_path.name + EXTRACTED_SUFFIX)
    if not sibling.exists():
        return None
    try:
        return sibling.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


# ---------- per-format readers -------------------------------------------


def _read_plaintext(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _read_pdf(path: Path) -> str:
    """Extract text from a PDF. Returns "" for image-only PDFs (no error)."""
    try:
        from pypdf import PdfReader
    except ImportError:
        log.warning("pypdf not installed; cannot extract %s", path)
        return ""
    reader = PdfReader(str(path))
    parts: list[str] = []
    for i, page in enumerate(reader.pages):
        try:
            parts.append(page.extract_text() or "")
        except Exception as exc:
            log.warning("pypdf page %d failed in %s: %s", i, path, exc)
    return "\n".join(p.strip() for p in parts if p and p.strip())


def _read_docx(path: Path) -> str:
    """Extract text from a .docx (Word) document."""
    try:
        from docx import Document
    except ImportError:
        log.warning("python-docx not installed; cannot extract %s", path)
        return ""
    doc = Document(str(path))
    paragraphs = [p.text for p in doc.paragraphs if p.text]
    # Table cells too; many real-world docx have content in tables.
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    if p.text:
                        paragraphs.append(p.text)
    return "\n".join(paragraphs)


# ---------- bulk helpers used by pipeline + draft_delivery ---------------


def render_for_prompt(
    attachment_paths: Iterable[Path],
    *,
    use_read_tool: bool = True,
) -> str:
    """Build the per-attachment block to inject into the draft prompt.

    With ``use_read_tool=True`` (default): include the file paths and a
    short ``PROMPT_PREVIEW_CHARS`` preview, and tell the AI it may use
    its Read tool on the paths to fetch the full text. Saves tokens.

    With ``use_read_tool=False``: paths are still listed (so the user can
    open them from the notification) but the prompt inlines the full
    extracted content up to ``MAX_EXTRACTED_CHARS`` per attachment.
    Slightly larger prompts; tighter security.

    Everything is wrapped in untrusted-content delimiters because prompt
    injection inside attachment text is the same threat as injection in
    the email body.
    """
    blocks: list[str] = []
    for path in attachment_paths:
        text = load_extracted(path)
        extracted_sibling: Path | None = None
        if text is not None:
            extracted_sibling = path.with_name(path.name + EXTRACTED_SUFFIX)

        header = [
            f"=== UNTRUSTED ATTACHMENT BEGIN: {path.name} ===",
            f"Original path:  {path}",
        ]
        if extracted_sibling is not None:
            header.append(f"Extracted text: {extracted_sibling}")
            if use_read_tool:
                header.append(
                    "(If the preview below is truncated or you need the full text, "
                    "use your Read tool on the Extracted text path.)"
                )
        if use_read_tool:
            header.append("--- preview ---")
            cap = PROMPT_PREVIEW_CHARS
            cap_label = "preview"
        else:
            header.append("--- full content ---")
            cap = MAX_EXTRACTED_CHARS
            cap_label = "full content"

        if text is None:
            preview = "(no extractable text; binary or unsupported format)"
        elif text.strip() == "":
            preview = "(extractor returned empty content; the file may be image-only)"
        else:
            preview = text[:cap]
            if len(text) > cap:
                preview += f"\n[... {cap_label} truncated at {cap} chars by JARLIS ...]"

        blocks.append(
            "\n".join(header) + f"\n{preview}\n=== UNTRUSTED ATTACHMENT END ==="
        )

    if not blocks:
        return ""
    return "\n\n".join(blocks)


def render_for_notification(attachment_paths: list[Path]) -> list[str]:
    """Build per-attachment lines for the notification email.

    Returns a list of pre-formatted lines (caller joins with newline) to
    keep formatting choices in one place.
    """
    out: list[str] = []
    for path in attachment_paths:
        out.append(f"  {path}")
        extracted = load_extracted(path)
        if extracted is None:
            continue
        sibling = path.with_name(path.name + EXTRACTED_SUFFIX)
        out.append(f"    -> {sibling}")
        if extracted.strip():
            preview = extracted[:NOTIFICATION_PER_ATTACHMENT_CHARS]
            if len(extracted) > NOTIFICATION_PER_ATTACHMENT_CHARS:
                preview += "\n[... truncated by JARLIS ...]"
            for line in preview.splitlines():
                out.append(f"      | {line}")
    return out
