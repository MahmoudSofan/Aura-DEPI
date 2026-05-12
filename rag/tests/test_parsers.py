"""Unit tests for the format-specific document parsers (T042)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rag import DocumentParseError
from rag.parsers import parse, parse_docx, parse_markdown, parse_pdf, parse_text


class _FakePdfCtx:
    """Drop-in stand-in for pdfplumber's ``with pdfplumber.open(...) as pdf``."""

    def __init__(self, pages: list[Any]) -> None:
        self.pages = pages

    def __enter__(self) -> _FakePdfCtx:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


def _patch_pdfplumber_open(monkeypatch: pytest.MonkeyPatch, page_texts: list[str]) -> None:
    pages = [SimpleNamespace(extract_text=lambda t=t: t) for t in page_texts]
    monkeypatch.setattr("pdfplumber.open", lambda path: _FakePdfCtx(pages))


# ---------------------------------------------------------------------------
# PDF.
# ---------------------------------------------------------------------------


def test_parse_pdf_extracts_text(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_pdfplumber_open(monkeypatch, ["Page one body.", "Page two body."])
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(b"%PDF-stub")

    parsed = parse_pdf(pdf_path)

    assert "Page one body." in parsed.text
    assert "Page two body." in parsed.text
    assert parsed.page_count == 2


def test_parse_pdf_rejects_image_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Image-only PDFs (no extractable text) raise DocumentParseError."""

    _patch_pdfplumber_open(monkeypatch, ["", ""])
    pdf_path = tmp_path / "scan.pdf"
    pdf_path.write_bytes(b"%PDF-stub")

    with pytest.raises(DocumentParseError):
        parse_pdf(pdf_path)


# ---------------------------------------------------------------------------
# DOCX.
# ---------------------------------------------------------------------------


def _write_docx(path: Path, paragraphs: list[str]) -> None:
    from docx import Document as DocxDocument

    doc = DocxDocument()
    for line in paragraphs:
        doc.add_paragraph(line)
    doc.save(str(path))


def test_parse_docx_extracts_text(tmp_path: Path) -> None:
    docx_path = tmp_path / "brief.docx"
    _write_docx(docx_path, ["Hello Aura.", "Free shipping over $75.", "Ships next day."])

    parsed = parse_docx(docx_path)

    assert "Hello Aura." in parsed.text
    assert "Free shipping over $75." in parsed.text


def test_parse_docx_rejects_empty(tmp_path: Path) -> None:
    docx_path = tmp_path / "empty.docx"
    _write_docx(docx_path, [])

    with pytest.raises(DocumentParseError):
        parse_docx(docx_path)


# ---------------------------------------------------------------------------
# TXT / MD.
# ---------------------------------------------------------------------------


def test_parse_text_utf8(tmp_path: Path) -> None:
    p = tmp_path / "notes.txt"
    p.write_text("ACME Mesh Pro Free shipping over $75.", encoding="utf-8")

    parsed = parse_text(p)

    assert "ACME Mesh Pro" in parsed.text


def test_parse_text_rejects_empty(tmp_path: Path) -> None:
    p = tmp_path / "empty.txt"
    p.write_bytes(b"")

    with pytest.raises(DocumentParseError):
        parse_text(p)


def test_parse_text_rejects_whitespace_only(tmp_path: Path) -> None:
    p = tmp_path / "blank.txt"
    p.write_text("   \n\t\n", encoding="utf-8")

    with pytest.raises(DocumentParseError):
        parse_text(p)


def test_parse_markdown(tmp_path: Path) -> None:
    p = tmp_path / "notes.md"
    p.write_text("# Heading\n\nBrand voice: confident, technical.", encoding="utf-8")

    parsed = parse_markdown(p)

    assert "Brand voice" in parsed.text


# ---------------------------------------------------------------------------
# Dispatch.
# ---------------------------------------------------------------------------


def test_dispatch_text(tmp_path: Path) -> None:
    p = tmp_path / "f.txt"
    p.write_text("dispatch test", encoding="utf-8")
    parsed = parse(p, "txt")
    assert parsed.text.strip() == "dispatch test"


def test_dispatch_pdf(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_pdfplumber_open(monkeypatch, ["dispatched"])
    p = tmp_path / "f.pdf"
    p.write_bytes(b"%PDF-stub")
    parsed = parse(p, "pdf")
    assert "dispatched" in parsed.text
