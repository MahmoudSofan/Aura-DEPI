"""Documents API — per-brand upload + list (FR-002, FR-003, FR-004).

* ``POST /api/v1/brands/{brand_id}/documents`` — multipart upload, runs the
  end-to-end ingest pipeline (parse → chunk → embed → upsert into Chroma).
* ``GET  /api/v1/brands/{brand_id}/documents`` — list ingested documents
  for a brand, newest first.

Errors map to HTTP status codes as specified by the OpenAPI contract:

* 404 — brand not found
* 409 — duplicate content for this brand (FR-004)
* 413 — file exceeds 50 MB cap
* 415 — unsupported file type (extension not in pdf/docx/txt/md)
* 422 — file parsed but produced no usable text (e.g., image-only PDF)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, cast

from agents.schemas import DocumentFormat, DocumentRecord
from backend.persistence.repository import BrandRepository, DocumentRepository
from backend.persistence.session import get_session
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, status
from rag import (
    DocumentParseError,
    DocumentTooLargeError,
    DuplicateDocumentError,
)
from rag.ingest import ingest_document
from sqlalchemy.orm import Session

logger = logging.getLogger("aura.api.documents")

router = APIRouter(tags=["documents"])

_MAX_BYTES = 52_428_800  # 50 MB
_EXT_TO_FORMAT: dict[str, DocumentFormat] = {
    "pdf": "pdf",
    "docx": "docx",
    "txt": "txt",
    "md": "md",
    "markdown": "md",
}


def _format_from_filename(filename: str) -> DocumentFormat | None:
    if "." not in filename:
        return None
    ext = filename.rsplit(".", 1)[1].lower().strip()
    return _EXT_TO_FORMAT.get(ext)


def _to_schema(doc: Any) -> DocumentRecord:
    return DocumentRecord(
        id=cast(str, doc.id),
        brand_id=cast(str, doc.brand_id),
        original_filename=cast(str, doc.original_filename),
        format=cast(DocumentFormat, doc.format),
        byte_size=cast(int, doc.byte_size),
        content_hash=cast(str, doc.content_hash),
        chunk_count=cast(int, doc.chunk_count),
        parse_status=cast("Any", doc.parse_status),
        parse_error=cast("str | None", doc.parse_error),
        created_at=cast(datetime, doc.created_at),
    )


def _resolve_chroma_client(request: Request) -> Any:
    """Reuse the runner's chroma factory so tests can monkey-patch one place."""

    runner = getattr(request.app.state, "campaign_runner", None)
    factory = getattr(runner, "_chroma_factory", None) if runner is not None else None
    if factory is not None:
        return factory()

    # Fallback when running without the runner (e.g., scripts) — match the
    # CHROMA_HOST / CHROMA_PORT env contract used elsewhere.
    import chromadb

    host = os.getenv("CHROMA_HOST")
    port_raw = os.getenv("CHROMA_PORT")
    if host and port_raw:
        try:
            return chromadb.HttpClient(host=host, port=int(port_raw))
        except Exception as exc:  # noqa: BLE001
            logger.warning("HttpClient init failed (%s); using EphemeralClient", exc)
    return chromadb.EphemeralClient()


@router.post(
    "/brands/{brand_id}/documents",
    status_code=status.HTTP_201_CREATED,
    response_model=DocumentRecord,
    summary="Upload a document for a brand",
    operation_id="uploadDocument",
)
async def upload_document(
    brand_id: str,
    request: Request,
    file: UploadFile,
    session: Session = Depends(get_session),
) -> DocumentRecord:
    if BrandRepository.get(session, brand_id) is None:
        raise HTTPException(status_code=404, detail=f"brand_id {brand_id!r} not found")

    filename = file.filename or "upload"
    fmt = _format_from_filename(filename)
    if fmt is None:
        raise HTTPException(status_code=415, detail=f"unsupported file type for {filename!r}")

    data = await file.read()
    byte_size = len(data)
    if byte_size <= 0:
        raise HTTPException(status_code=422, detail=f"{filename}: file is empty")
    if byte_size > _MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"{filename}: {byte_size} bytes exceeds 50 MB cap",
        )

    chroma_client = _resolve_chroma_client(request)
    try:
        result = ingest_document(
            brand_id=brand_id,
            file_bytes=data,
            original_filename=filename,
            format=fmt,
            session=session,
            chroma_client=chroma_client,
        )
    except DuplicateDocumentError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DocumentTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except DocumentParseError as exc:
        # The rejected row was committed inside ingest_document; commit it
        # here so the transaction lands before we return 422.
        session.commit()
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    session.commit()
    logger.info(
        "brand %s: ingested document %s (%d chunks)",
        brand_id,
        result.record.id,
        result.chunk_count,
    )
    return result.record


@router.get(
    "/brands/{brand_id}/documents",
    response_model=list[DocumentRecord],
    summary="List a brand's ingested documents (newest first)",
    operation_id="listDocuments",
)
def list_documents(
    brand_id: str,
    session: Session = Depends(get_session),
) -> list[DocumentRecord]:
    if BrandRepository.get(session, brand_id) is None:
        raise HTTPException(status_code=404, detail=f"brand_id {brand_id!r} not found")
    docs = DocumentRepository.list_for_brand(session, brand_id)
    return [_to_schema(d) for d in docs]


__all__ = ["router"]
