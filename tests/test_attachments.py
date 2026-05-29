"""Tests for attachment text extraction."""

from __future__ import annotations

import tempfile
from pathlib import Path

from jarlis import attachments


def test_can_extract_known_extensions() -> None:
    assert attachments.can_extract("report.pdf")
    assert attachments.can_extract("Brief.PDF")
    assert attachments.can_extract("memo.docx")
    assert attachments.can_extract("notes.txt")
    assert attachments.can_extract("readme.md")
    assert attachments.can_extract("data.csv")
    assert not attachments.can_extract("photo.jpg")
    assert not attachments.can_extract("archive.zip")
    assert not attachments.can_extract("script.exe")


def test_extract_plaintext_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as t:
        path = Path(t) / "memo.txt"
        path.write_text("hello world\nline two", encoding="utf-8")
        out = attachments.extract_text(path)
        assert out == "hello world\nline two"


def test_extract_unknown_returns_none() -> None:
    with tempfile.TemporaryDirectory() as t:
        path = Path(t) / "image.jpg"
        path.write_bytes(b"\xff\xd8\xff\xe0")
        assert attachments.extract_text(path) is None


def test_extract_and_save_writes_sibling() -> None:
    with tempfile.TemporaryDirectory() as t:
        att = Path(t) / "memo.txt"
        att.write_text("the content", encoding="utf-8")
        out = attachments.extract_and_save(att)
        assert out is not None
        assert out.name == "memo.txt.extracted.txt"
        assert out.read_text(encoding="utf-8") == "the content"


def test_extract_and_save_skips_unknown_types() -> None:
    with tempfile.TemporaryDirectory() as t:
        att = Path(t) / "image.jpg"
        att.write_bytes(b"\xff\xd8")
        assert attachments.extract_and_save(att) is None
        assert not (att.parent / "image.jpg.extracted.txt").exists()


def test_extract_caps_long_text() -> None:
    with tempfile.TemporaryDirectory() as t:
        att = Path(t) / "huge.txt"
        att.write_text("A" * (attachments.MAX_EXTRACTED_CHARS + 1000), encoding="utf-8")
        out = attachments.extract_and_save(att)
        assert out is not None
        body = out.read_text(encoding="utf-8")
        assert "truncated by JARLIS" in body
        assert body.count("A") <= attachments.MAX_EXTRACTED_CHARS + 100


def test_load_extracted_returns_none_when_missing() -> None:
    with tempfile.TemporaryDirectory() as t:
        att = Path(t) / "no_sibling.txt"
        att.write_text("not extracted yet", encoding="utf-8")
        assert attachments.load_extracted(att) is None


def test_render_for_prompt_wraps_in_untrusted_delimiters() -> None:
    with tempfile.TemporaryDirectory() as t:
        att = Path(t) / "report.txt"
        att.write_text("the budget is 5000 euros for next quarter", encoding="utf-8")
        attachments.extract_and_save(att)
        out = attachments.render_for_prompt([att])
        assert "UNTRUSTED ATTACHMENT BEGIN: report.txt" in out
        assert "UNTRUSTED ATTACHMENT END" in out
        assert "the budget is 5000 euros" in out


def test_render_for_prompt_includes_paths_for_read_tool() -> None:
    with tempfile.TemporaryDirectory() as t:
        att = Path(t) / "report.txt"
        att.write_text("the budget", encoding="utf-8")
        attachments.extract_and_save(att)
        out = attachments.render_for_prompt([att])
        # Both original and extracted-text paths must be present so a
        # Read-tool-enabled backend can fetch them.
        assert f"Original path:  {att}" in out
        assert f"Extracted text: {att}{attachments.EXTRACTED_SUFFIX}" in out
        # The instruction hint mentions the Read tool option.
        assert "Read tool" in out


def test_render_for_prompt_caps_preview_at_short_length() -> None:
    with tempfile.TemporaryDirectory() as t:
        att = Path(t) / "huge.txt"
        att.write_text("X" * 20_000, encoding="utf-8")
        attachments.extract_and_save(att)
        out = attachments.render_for_prompt([att])
        assert "truncated" in out
        # Inline preview is much smaller than full extraction now
        assert out.count("X") <= attachments.PROMPT_PREVIEW_CHARS + 100


def test_render_for_prompt_no_read_tool_inlines_full_content() -> None:
    """When use_read_tool=False, the prompt inlines the full extracted text."""
    with tempfile.TemporaryDirectory() as t:
        att = Path(t) / "report.txt"
        # Bigger than PROMPT_PREVIEW_CHARS but smaller than MAX_EXTRACTED_CHARS
        body = "A" * (attachments.PROMPT_PREVIEW_CHARS + 5000)
        att.write_text(body, encoding="utf-8")
        attachments.extract_and_save(att)
        out = attachments.render_for_prompt([att], use_read_tool=False)
        # The full content fits without truncation
        assert "truncated" not in out
        assert out.count("A") >= attachments.PROMPT_PREVIEW_CHARS + 1000
        # Read-tool hint should NOT be present
        assert "Read tool" not in out
        # The content marker is "full content" not "preview"
        assert "full content" in out


def test_render_for_prompt_attachment_without_extraction_still_lists_path() -> None:
    """Even unsupported types (jpg, zip) get the original path in the prompt
    so the AI can attempt a Read tool call on them if needed."""
    with tempfile.TemporaryDirectory() as t:
        att = Path(t) / "image.jpg"
        att.write_bytes(b"\xff\xd8")  # no extraction sibling
        out = attachments.render_for_prompt([att])
        assert "UNTRUSTED ATTACHMENT BEGIN: image.jpg" in out
        assert f"Original path:  {att}" in out
        # Should NOT advertise an extracted-text path that doesn't exist
        assert "Extracted text:" not in out
        assert "no extractable text" in out


def test_render_for_notification_shows_full_path_and_preview() -> None:
    with tempfile.TemporaryDirectory() as t:
        att = Path(t) / "memo.txt"
        att.write_text("Top secret budget details:\n2026 plan\nQ1 revenue 100k", encoding="utf-8")
        attachments.extract_and_save(att)
        lines = attachments.render_for_notification([att])
        joined = "\n".join(lines)
        assert str(att) in joined
        assert str(att) + ".extracted.txt" in joined
        assert "Top secret budget" in joined
        assert "Q1 revenue 100k" in joined


def test_render_for_notification_attachment_without_extraction() -> None:
    with tempfile.TemporaryDirectory() as t:
        att = Path(t) / "photo.jpg"
        att.write_bytes(b"\xff\xd8")
        lines = attachments.render_for_notification([att])
        # No extracted text → still shows the path
        assert any(str(att) in line for line in lines)
        # No extracted-text path line
        assert not any("extracted.txt" in line for line in lines)


def test_render_for_notification_includes_translation_block(tmp_path):
    from jarlis import attachments
    att = tmp_path / "report.txt"
    att.write_text("本文", encoding="utf-8")
    (tmp_path / ("report.txt" + attachments.EXTRACTED_SUFFIX)).write_text(
        "本文の抽出テキスト", encoding="utf-8",
    )
    lines = attachments.render_for_notification(
        [att],
        translations={str(att): "Texte traduit en francais."},
        translation_label="traduction en fr",
    )
    blob = "\n".join(lines)
    assert str(att) in blob                       # full path
    assert "| 本文の抽出テキスト" in blob          # original preview
    assert "traduction en fr" in blob             # label
    assert "~ Texte traduit en francais." in blob  # translated preview


def test_render_for_notification_no_translation_when_absent(tmp_path):
    from jarlis import attachments
    att = tmp_path / "doc.txt"
    att.write_text("x", encoding="utf-8")
    (tmp_path / ("doc.txt" + attachments.EXTRACTED_SUFFIX)).write_text("hello", encoding="utf-8")
    lines = attachments.render_for_notification([att])  # no translations
    blob = "\n".join(lines)
    assert "~" not in blob
    assert "traduction" not in blob
