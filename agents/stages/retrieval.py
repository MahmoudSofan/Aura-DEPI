"""Retrieval stage — per-brand Chroma similarity search.

T052 (User Story 2) implementation: queries the brand-scoped collection
``brand_{brand_id}`` for top-K chunks against the brief + audience, and
synthesises a one-line ``brand_voice`` from the highest-ranked chunk
(LLM-free — just a snippet of the top result).

Both ingest and retrieval embed via :func:`rag.embeddings.embed_batch`
so the query and the indexed vectors share the same vector space (no
silent reliance on Chroma's default embedding function).

Empty fallback (``RetrievedContext(chunks=[], brand_voice="")``) only
when the brand collection has zero items — i.e., the brand has not yet
ingested any documents (the US1 baseline path). Hard-fails on Chroma
transport / RPC errors per FR-021.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from agents.schemas import CampaignRequest, Chunk, ModelCall, RetrievedContext
from agents.stages import StageError

logger = logging.getLogger("aura.stages.retrieval")

_DEFAULT_TOP_K = 5


def _collection_name(brand_id: str) -> str:
    return f"brand_{brand_id}"


def _top_k() -> int:
    raw = os.getenv("AURA_RETRIEVAL_TOP_K")
    if not raw:
        return _DEFAULT_TOP_K
    try:
        return max(1, int(raw))
    except ValueError:
        return _DEFAULT_TOP_K


def _build_query(request: CampaignRequest) -> str:
    return f"{request.brief} {request.target_audience}"


def _embed_query(text: str) -> list[float]:
    """Embed the query via the same wrapper ingest uses, so the index and
    the query live in the same vector space."""

    from rag.embeddings import embed_batch

    vectors = embed_batch([text])
    if not vectors:
        raise StageError("embedding: query produced no vector")
    return vectors[0]


def run_retrieval(
    request: CampaignRequest,
    *,
    chroma_client: Any,
) -> tuple[RetrievedContext, list[ModelCall]]:
    """Query the brand-scoped Chroma collection.

    Empty fallback when the collection is missing or has zero items.
    Raises :class:`StageError` on any Chroma transport failure (FR-021
    hard-fail class).
    """

    name = _collection_name(request.brand_id)
    started_at = time.monotonic()

    try:
        collection = chroma_client.get_or_create_collection(name=name)
    except Exception as exc:  # noqa: BLE001
        raise StageError(f"chroma get_or_create_collection failed: {exc}") from exc

    try:
        count = collection.count()
    except Exception as exc:  # noqa: BLE001
        raise StageError(f"chroma collection.count failed: {exc}") from exc

    if count == 0:
        latency_ms = max(0, int((time.monotonic() - started_at) * 1000))
        logger.info("retrieval: brand %s has zero indexed chunks; empty fallback", request.brand_id)
        return (
            RetrievedContext(chunks=[], brand_voice=""),
            [
                ModelCall(
                    provider="huggingface",
                    model="all-MiniLM-L6-v2",
                    op="empty-collection-skip",
                    latency_ms=latency_ms,
                )
            ],
        )

    query_text = _build_query(request)
    try:
        query_vector = _embed_query(query_text)
    except StageError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise StageError(f"embedding: query failed: {exc}") from exc

    try:
        results = collection.query(
            query_embeddings=[query_vector],
            n_results=_top_k(),
        )
    except Exception as exc:  # noqa: BLE001
        raise StageError(f"chroma collection.query failed: {exc}") from exc

    latency_ms = max(0, int((time.monotonic() - started_at) * 1000))

    documents_2d = results.get("documents") if isinstance(results, dict) else None
    metadatas_2d = results.get("metadatas") if isinstance(results, dict) else None
    documents = documents_2d[0] if documents_2d else []
    metadatas = metadatas_2d[0] if metadatas_2d else []

    chunks: list[Chunk] = []
    for idx, text in enumerate(documents):
        if not text:
            continue
        meta = metadatas[idx] if idx < len(metadatas) and isinstance(metadatas[idx], dict) else {}
        source = meta.get("source_filename") or meta.get("document_id") or f"{name}:chunk:{idx}"
        chunks.append(Chunk(text=text, source=str(source)))

    # Brand voice — a one-line snippet of the highest-ranked chunk.
    brand_voice = ""
    if chunks:
        first_line = chunks[0].text.strip().splitlines()[0] if chunks[0].text.strip() else ""
        brand_voice = first_line[:200] if first_line else chunks[0].text[:200]

    return (
        RetrievedContext(chunks=chunks, brand_voice=brand_voice),
        [
            ModelCall(
                provider="huggingface",
                model="all-MiniLM-L6-v2",
                op="similarity-search",
                latency_ms=latency_ms,
            )
        ],
    )


__all__ = ["run_retrieval"]
