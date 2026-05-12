"""RAG ingestion package: parsers, chunking, embeddings, and brand-scoped ingest.

Public surface (Phase 4 / User Story 2):

* :class:`DocumentParseError` — raised by parsers when input is unparseable.
* :class:`DuplicateDocumentError` — raised by ingest when a brand already has
  a document with the same SHA-256 content hash (FR-004 dedup).
* :class:`DocumentTooLargeError` — raised by ingest when the file exceeds
  the 50 MB cap.
* :func:`parse` — dispatch to the format-specific parser.
* :func:`chunk` — token-based chunker with overlap.
* :func:`embed_batch` — sentence-transformers wrapper.
* :func:`ingest_document` — end-to-end: hash → dedup → persist → parse →
  chunk → embed → upsert into the brand-scoped Chroma collection.
"""

from __future__ import annotations


class DocumentParseError(Exception):
    """Raised when an uploaded document cannot be parsed (empty, image-only, etc.)."""


class DuplicateDocumentError(Exception):
    """Raised when ``(brand_id, content_hash)`` already exists (FR-004)."""


class DocumentTooLargeError(Exception):
    """Raised when an upload exceeds the 50 MB per-file cap (Assumptions §size)."""


__all__ = [
    "DocumentParseError",
    "DocumentTooLargeError",
    "DuplicateDocumentError",
]
