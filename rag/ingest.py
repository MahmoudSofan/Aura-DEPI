"""End-to-end document ingest for User Story 2.

Steps (per data-model.md §"Document parse lifecycle"):

1. Compute SHA-256 of the raw bytes.
2. Reject if ``(brand_id, content_hash)`` already exists (FR-004 dedup).
3. Reject if size > 50 MB (Assumptions).
4. Persist the file under ``backend/data/uploads/{brand_id}/{document_id}.{ext}``.
5. Parse via :func:`rag.parsers.parse`.
6. Chunk via :func:`rag.chunking.chunk`.
7. Embed via :func:`rag.embeddings.embed_batch`.
8. Upsert into Chroma collection ``brand_{brand_id}``.
9. INSERT a ``documents`` row with ``parse_status='parsed'`` and ``chunk_count``.

On parse failure, the row is still inserted with ``parse_status='rejected'``
and the file is retained on disk for audit.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from agents.schemas import DocumentFormat, DocumentRecord
from backend.persistence.repository import DocumentRepository
from sqlalchemy.orm import Session
from ulid import ULID

from rag import (
    DocumentParseError,
    DocumentTooLargeError,
    DuplicateDocumentError,
)
from rag.chunking import Chunk, chunk
from rag.embeddings import embed_batch
from rag.parsers import parse

logger = logging.getLogger("aura.rag.ingest")

_MAX_BYTES = 52_428_800  # 50 MB
_FORMAT_EXTENSIONS: dict[DocumentFormat, str] = {
    "pdf": "pdf",
    "docx": "docx",
    "txt": "txt",
    "md": "md",
}


@dataclass
class IngestResult:
    """Return value of :func:`ingest_document` mirroring the API ``Document`` payload."""

    record: DocumentRecord
    chunk_count: int


def _data_root() -> Path:
    return Path(os.getenv("AURA_DATA_DIR", "backend/data"))


def _uploads_root() -> Path:
    return _data_root() / "uploads"


def _collection_name(brand_id: str) -> str:
    return f"brand_{brand_id}"


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _save_file(brand_id: str, document_id: str, ext: str, data: bytes) -> Path:
    target_dir = _uploads_root() / brand_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{document_id}.{ext}"
    target_path.write_bytes(data)
    return target_path


def _upsert_chunks(
    chroma_client: Any,
    *,
    brand_id: str,
    document_id: str,
    source_filename: str,
    chunks: list[Chunk],
    embeddings: list[list[float]],
) -> None:
    if not chunks:
        return
    collection = chroma_client.get_or_create_collection(name=_collection_name(brand_id))
    ids = [f"{document_id}:{c.chunk_index}" for c in chunks]
    metadatas = [
        {
            "document_id": document_id,
            "chunk_index": c.chunk_index,
            "source_filename": source_filename,
            "page": c.page if c.page is not None else -1,
        }
        for c in chunks
    ]
    documents = [c.text for c in chunks]
    collection.upsert(
        ids=ids,
        documents=documents,
        metadatas=metadatas,
        embeddings=embeddings,
    )


def ingest_document(
    *,
    brand_id: str,
    file_bytes: bytes,
    original_filename: str,
    format: DocumentFormat,
    session: Session,
    chroma_client: Any,
) -> IngestResult:
    """Run the full ingest pipeline for a single uploaded document.

    Raises:
        DuplicateDocumentError: if a document with the same SHA-256 already
            exists for this brand.
        DocumentTooLargeError: if ``len(file_bytes) > 50 MB``.
        DocumentParseError: if parsing produced no usable text. The
            ``documents`` row is still inserted with
            ``parse_status='rejected'`` so operators can audit.
    """

    if format not in _FORMAT_EXTENSIONS:
        raise DocumentParseError(f"unsupported format: {format!r}")

    byte_size = len(file_bytes)
    if byte_size <= 0:
        raise DocumentParseError(f"{original_filename}: file is empty")
    if byte_size > _MAX_BYTES:
        raise DocumentTooLargeError(f"{original_filename}: {byte_size} bytes exceeds 50 MB cap")

    content_hash = _sha256_hex(file_bytes)
    if DocumentRepository.get_by_hash(session, brand_id, content_hash) is not None:
        raise DuplicateDocumentError(
            f"document with content_hash {content_hash} already exists for brand {brand_id}"
        )

    document_id = str(ULID())
    ext = _FORMAT_EXTENSIONS[format]
    storage_path = _save_file(brand_id, document_id, ext, file_bytes)
    storage_path_rel = str(storage_path.relative_to(_data_root()))

    try:
        parsed = parse(storage_path, format)
    except DocumentParseError as exc:
        logger.warning("ingest: parse failed for %s: %s", original_filename, exc)
        DocumentRepository.create(
            session,
            document_id=document_id,
            brand_id=brand_id,
            original_filename=original_filename,
            format=format,
            byte_size=byte_size,
            content_hash=content_hash,
            storage_path=storage_path_rel,
            chunk_count=0,
            parse_status="rejected",
            parse_error=str(exc),
        )
        session.flush()
        raise

    chunks = chunk(parsed, document_id=document_id)
    if not chunks:
        # Edge case: parser succeeded but tokenizer produced zero chunks.
        DocumentRepository.create(
            session,
            document_id=document_id,
            brand_id=brand_id,
            original_filename=original_filename,
            format=format,
            byte_size=byte_size,
            content_hash=content_hash,
            storage_path=storage_path_rel,
            chunk_count=0,
            parse_status="rejected",
            parse_error="parser produced no chunks",
        )
        session.flush()
        raise DocumentParseError(f"{original_filename}: parser produced no chunks")

    embeddings = embed_batch([c.text for c in chunks])
    _upsert_chunks(
        chroma_client,
        brand_id=brand_id,
        document_id=document_id,
        source_filename=original_filename,
        chunks=chunks,
        embeddings=embeddings,
    )

    record = DocumentRepository.create(
        session,
        document_id=document_id,
        brand_id=brand_id,
        original_filename=original_filename,
        format=format,
        byte_size=byte_size,
        content_hash=content_hash,
        storage_path=storage_path_rel,
        chunk_count=len(chunks),
        parse_status="parsed",
        parse_error=None,
    )
    session.flush()

    return IngestResult(
        record=DocumentRecord(
            id=record.id,
            brand_id=record.brand_id,
            original_filename=record.original_filename,
            format=cast(DocumentFormat, record.format),
            byte_size=record.byte_size,
            content_hash=record.content_hash,
            chunk_count=record.chunk_count,
            parse_status=cast("Any", record.parse_status),
            parse_error=record.parse_error,
            created_at=record.created_at,
        ),
        chunk_count=len(chunks),
    )


__all__ = ["IngestResult", "ingest_document"]
