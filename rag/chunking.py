"""Token-based chunker (research.md §9).

Default: ~500-token chunks with 50-token overlap, computed against the
``cl100k_base`` tokenizer (the OpenAI tokenizer used by GPT-4o-mini).
Each chunk carries provenance fields: a ``source`` of the form
``"{document_id}:{chunk_index}"`` and an optional ``page`` hint when the
underlying parser supplied page boundaries.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from rag.parsers import ParsedDocument

logger = logging.getLogger("aura.rag.chunking")

_DEFAULT_CHUNK_SIZE = 500
_DEFAULT_OVERLAP = 50


@dataclass
class Chunk:
    """A single chunk produced by :func:`chunk`.

    ``text`` is the chunk text. ``source`` is ``"{document_id}:{chunk_index}"``
    so a downstream consumer can trace any chunk back to its source. ``page``
    is set only when the parser supplied page boundaries (PDFs).
    """

    text: str
    source: str
    chunk_index: int
    page: int | None = None


def _get_encoder() -> object:
    """Lazily load the cl100k_base tokenizer.

    Wrapped in a function so import of the module doesn't pay the cost.
    """

    import tiktoken

    return tiktoken.get_encoding("cl100k_base")


def chunk(
    parsed: ParsedDocument,
    *,
    document_id: str,
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
    overlap: int = _DEFAULT_OVERLAP,
) -> list[Chunk]:
    """Chunk ``parsed.text`` into ~``chunk_size``-token pieces with overlap."""

    if chunk_size <= 0:
        raise ValueError("chunk_size must be > 0")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be in [0, chunk_size)")

    encoder = _get_encoder()
    tokens: list[int] = encoder.encode(parsed.text)  # type: ignore[attr-defined]

    if not tokens:
        return []

    chunks: list[Chunk] = []
    step = chunk_size - overlap
    idx = 0
    pos = 0
    while pos < len(tokens):
        token_slice = tokens[pos : pos + chunk_size]
        text_slice = encoder.decode(token_slice)  # type: ignore[attr-defined]
        if text_slice.strip():
            chunks.append(
                Chunk(
                    text=text_slice,
                    source=f"{document_id}:{idx}",
                    chunk_index=idx,
                    page=None,
                )
            )
            idx += 1
        if pos + chunk_size >= len(tokens):
            break
        pos += step

    return chunks


__all__ = ["Chunk", "chunk"]
