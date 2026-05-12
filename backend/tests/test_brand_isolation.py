"""Brand-isolation invariant: chunks never leak across brands (T039, FR-005)."""

from __future__ import annotations

from typing import Any

from agents.schemas import CampaignRequest
from agents.stages.retrieval import run_retrieval
from fastapi.testclient import TestClient


def _create_brand(api_client: TestClient, display_name: str) -> str:
    return str(api_client.post("/api/v1/brands", json={"display_name": display_name}).json()["id"])


def test_chunks_never_leak_across_brands(
    api_client: TestClient,
    stub_embeddings: None,
    shared_chroma: Any,
) -> None:
    brand_a = _create_brand(api_client, "Brand Alpha")
    brand_b = _create_brand(api_client, "Brand Beta")

    payload_a = (
        b"Brand Alpha is a sustainable footwear maker.\n"
        b"Distinctive marker phrase: ALPHA-MARK-7421.\n"
        b"Free shipping over $75."
    )
    payload_b = (
        b"Brand Beta sells smart home electronics.\n"
        b"Distinctive marker phrase: BETA-MARK-9999.\n"
        b"Ships next day in the contiguous US."
    )

    a_resp = api_client.post(
        f"/api/v1/brands/{brand_a}/documents",
        files={"file": ("alpha.txt", payload_a, "text/plain")},
    )
    b_resp = api_client.post(
        f"/api/v1/brands/{brand_b}/documents",
        files={"file": ("beta.txt", payload_b, "text/plain")},
    )
    assert a_resp.status_code == 201
    assert b_resp.status_code == 201

    request = CampaignRequest(
        brief="What is the distinctive marker phrase?",
        platform="instagram",
        brand_id=brand_a,
        target_audience="any audience",
    )

    # 100 retrieval calls scoped to brand A.
    leaked = 0
    total_chunks = 0
    for _ in range(100):
        retrieved, _ = run_retrieval(request, chroma_client=shared_chroma)
        total_chunks += len(retrieved.chunks)
        for chunk in retrieved.chunks:
            if "BETA-MARK-9999" in chunk.text:
                leaked += 1

    assert total_chunks > 0, "retrieval returned zero chunks across 100 calls"
    assert leaked == 0, f"brand-A retrieval surfaced {leaked} chunk(s) from brand B"
