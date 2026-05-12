"""Unit tests for the token-based chunker (T043)."""

from __future__ import annotations

import pytest

from rag.chunking import Chunk, chunk
from rag.parsers import ParsedDocument


def _parsed(text: str) -> ParsedDocument:
    return ParsedDocument(text=text, page_count=None, parse_metadata={})


def test_chunk_short_text_yields_single_chunk() -> None:
    parsed = _parsed("Short brand brief.")
    chunks = chunk(parsed, document_id="01HXDOC")

    assert len(chunks) == 1
    assert chunks[0].text.strip() == "Short brand brief."
    assert chunks[0].source == "01HXDOC:0"
    assert chunks[0].chunk_index == 0


def test_chunk_long_text_splits_with_overlap() -> None:
    # 'word ' repeated produces ~600 tokens (cl100k tokenises 'word' as one
    # token plus a leading-space variant), comfortably above one chunk.
    parsed = _parsed(("word " * 1200).strip())
    chunks = chunk(parsed, document_id="01HXLONG", chunk_size=500, overlap=50)

    assert len(chunks) >= 2
    assert chunks[0].source == "01HXLONG:0"
    assert chunks[1].source == "01HXLONG:1"
    # All chunks carry provenance.
    for c in chunks:
        assert c.source.startswith("01HXLONG:")


def test_chunk_count_is_deterministic() -> None:
    parsed = _parsed("alpha beta gamma " * 800)
    a = chunk(parsed, document_id="A", chunk_size=200, overlap=20)
    b = chunk(parsed, document_id="B", chunk_size=200, overlap=20)
    assert len(a) == len(b)


def test_chunk_empty_text_returns_empty() -> None:
    parsed = _parsed("")
    chunks = chunk(parsed, document_id="X")
    assert chunks == []


def test_chunk_invalid_overlap_raises() -> None:
    parsed = _parsed("hello world")
    with pytest.raises(ValueError):
        chunk(parsed, document_id="X", chunk_size=100, overlap=100)
    with pytest.raises(ValueError):
        chunk(parsed, document_id="X", chunk_size=0, overlap=0)


def test_chunk_dataclass_fields() -> None:
    parsed = _parsed("brief content here")
    [c] = chunk(parsed, document_id="01HXY")
    assert isinstance(c, Chunk)
    assert c.chunk_index == 0
    assert c.page is None
