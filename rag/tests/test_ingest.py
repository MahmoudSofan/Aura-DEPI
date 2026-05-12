"""End-to-end ingest pipeline tests (T044)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from backend.persistence.repository import BrandRepository, DocumentRepository
from sqlalchemy.orm import Session

from rag import (
    DocumentParseError,
    DocumentTooLargeError,
    DuplicateDocumentError,
)
from rag.ingest import ingest_document


@pytest.fixture
def seeded_brand(session: Session) -> str:
    brand_id = "01HXBRANDINGEST00000000000"
    BrandRepository.create(session, brand_id=brand_id, display_name="Ingest Test Brand")
    session.commit()
    return brand_id


def test_ingest_happy_path_persists_and_indexes(
    session: Session,
    seeded_brand: str,
    chroma_client: Any,
    stub_embeddings: None,
    tmp_data_dir: Path,
) -> None:
    payload = b"ACME Mesh Pro\nFree shipping over $75.\nShips next day in the contiguous US.\n"
    result = ingest_document(
        brand_id=seeded_brand,
        file_bytes=payload,
        original_filename="brand.txt",
        format="txt",
        session=session,
        chroma_client=chroma_client,
    )
    session.commit()

    assert result.chunk_count > 0
    record = result.record
    assert record.parse_status == "parsed"
    assert record.brand_id == seeded_brand
    assert record.chunk_count > 0
    # Persisted row.
    docs = DocumentRepository.list_for_brand(session, seeded_brand)
    assert len(docs) == 1
    # Chunks landed in the brand-scoped collection.
    coll = chroma_client.get_or_create_collection(name=f"brand_{seeded_brand}")
    assert coll.count() >= 1


def test_ingest_rejects_duplicate_content_per_brand(
    session: Session,
    seeded_brand: str,
    chroma_client: Any,
    stub_embeddings: None,
) -> None:
    payload = b"Brand voice: confident, technical."

    ingest_document(
        brand_id=seeded_brand,
        file_bytes=payload,
        original_filename="voice.txt",
        format="txt",
        session=session,
        chroma_client=chroma_client,
    )
    session.commit()

    with pytest.raises(DuplicateDocumentError):
        ingest_document(
            brand_id=seeded_brand,
            file_bytes=payload,
            original_filename="voice-copy.txt",
            format="txt",
            session=session,
            chroma_client=chroma_client,
        )


def test_ingest_two_documents_accumulate(
    session: Session,
    seeded_brand: str,
    chroma_client: Any,
    stub_embeddings: None,
) -> None:
    a = ingest_document(
        brand_id=seeded_brand,
        file_bytes=b"Brand A claim: free shipping.",
        original_filename="a.txt",
        format="txt",
        session=session,
        chroma_client=chroma_client,
    )
    session.commit()
    b = ingest_document(
        brand_id=seeded_brand,
        file_bytes=b"Brand B claim: ships next day.",
        original_filename="b.txt",
        format="txt",
        session=session,
        chroma_client=chroma_client,
    )
    session.commit()

    assert a.record.id != b.record.id
    coll = chroma_client.get_or_create_collection(name=f"brand_{seeded_brand}")
    assert coll.count() == a.chunk_count + b.chunk_count


def test_ingest_rejects_oversize(
    session: Session,
    seeded_brand: str,
    chroma_client: Any,
    stub_embeddings: None,
) -> None:
    huge = b"x" * (52_428_800 + 1)
    with pytest.raises(DocumentTooLargeError):
        ingest_document(
            brand_id=seeded_brand,
            file_bytes=huge,
            original_filename="huge.txt",
            format="txt",
            session=session,
            chroma_client=chroma_client,
        )


def test_ingest_persists_rejected_row_on_parse_failure(
    session: Session,
    seeded_brand: str,
    chroma_client: Any,
    stub_embeddings: None,
) -> None:
    """Whitespace-only file → DocumentParseError but row is persisted with parse_status='rejected'."""

    with pytest.raises(DocumentParseError):
        ingest_document(
            brand_id=seeded_brand,
            file_bytes=b"   \n\n   \n",
            original_filename="blank.txt",
            format="txt",
            session=session,
            chroma_client=chroma_client,
        )
    # The ingest function calls session.flush() on the rejected row;
    # commit it here to land the transaction.
    session.commit()

    docs = DocumentRepository.list_for_brand(session, seeded_brand)
    assert len(docs) == 1
    assert docs[0].parse_status == "rejected"
    assert docs[0].chunk_count == 0
    assert docs[0].parse_error
