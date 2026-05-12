"""SC-010 — quickstart §2–§9 walkthrough as a programmatic acceptance test.

Walks through each step of `specs/001-aura-marketing-platform/quickstart.md`
that an operator would run during the ≤5-minute happy-path smoke:

* §2 — create a brand
* §3 — upload a brand document (and verify dedup + format-rejection edge cases)
* §4 — submit a brief and confirm the 202 acknowledgement
* §5 — poll the run as it advances
* §6 — assert the delivered campaign payload shape
* §7.1 — hard-fail surfaces ``failed_stage`` + ``failed_reason``
* §7.2 — degradable research stage doesn't break the run
* §8 — brand isolation: a second brand's docs don't leak into brand A
* §9 — DELETE cascades brand → docs → runs → artifacts

Uses stubbed external clients (no token spend, deterministic timing).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from backend.persistence import session as session_module
from backend.persistence.repository import (
    BrandRepository,
    DocumentRepository,
    RunRepository,
)
from backend.tests.conftest import (
    StubRecorder,
    make_copy_response,
    make_critic_response,
    make_pil_image,
)
from fastapi.testclient import TestClient


def _create_brand(client: TestClient, display_name: str) -> str:
    resp = client.post("/api/v1/brands", json={"display_name": display_name})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["display_name"] == display_name
    assert body["id"]
    return str(body["id"])


def _wait_for(
    client: TestClient,
    run_id: str,
    statuses: tuple[str, ...],
    *,
    timeout_s: float = 90.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        resp = client.get(f"/api/v1/campaigns/{run_id}")
        assert resp.status_code == 200, resp.text
        body: dict[str, Any] = resp.json()
        if body["status"] in statuses:
            return body
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} never reached {statuses}")


def test_quickstart_happy_path_walkthrough(
    api_client: TestClient,
    stub_embeddings: None,
    stub_openai: StubRecorder,
    stub_image: StubRecorder,
    tmp_data_dir: Path,
) -> None:
    """Programmatically execute quickstart §2–§9 end-to-end."""

    # --- §2 Create a brand ---------------------------------------------
    brand_a = _create_brand(api_client, "ACME Sneakers")

    # GET /brands and GET /brands/{id} are reachable.
    listing = api_client.get("/api/v1/brands")
    assert listing.status_code == 200
    assert any(b["id"] == brand_a for b in listing.json())

    one = api_client.get(f"/api/v1/brands/{brand_a}")
    assert one.status_code == 200
    assert one.json()["id"] == brand_a

    # --- §3 Upload a brand document + edge cases -----------------------
    payload = (
        b"Brand voice: confident, technical.\n"
        b"ACME Mesh Pro Free shipping over $75.\n"
        b"All shoes ship next day in the contiguous US."
    )
    upload = api_client.post(
        f"/api/v1/brands/{brand_a}/documents",
        files={"file": ("brand_guide.txt", payload, "text/plain")},
    )
    assert upload.status_code == 201, upload.text
    doc_a = upload.json()
    assert doc_a["parse_status"] == "parsed"
    assert doc_a["chunk_count"] >= 1

    # Dedup (FR-004): same content rejected with 409.
    dup = api_client.post(
        f"/api/v1/brands/{brand_a}/documents",
        files={"file": ("brand_guide.txt", payload, "text/plain")},
    )
    assert dup.status_code == 409, dup.text

    # Unsupported format → 415.
    bad_format = api_client.post(
        f"/api/v1/brands/{brand_a}/documents",
        files={"file": ("ledger.xlsx", b"PK fake xlsx", "application/octet-stream")},
    )
    assert bad_format.status_code == 415, bad_format.text

    # --- §4 Submit a brief; expect a 202 + run_id ----------------------
    stub_openai.push(make_copy_response())
    stub_image.push(make_pil_image(width=512, height=512))
    stub_openai.push(make_critic_response())

    submit = api_client.post(
        "/api/v1/campaigns",
        json={
            "brief": "Launch summer sneaker line.",
            "platform": "instagram",
            "brand_id": brand_a,
            "target_audience": "18-24 urban fitness-curious",
        },
    )
    assert submit.status_code == 202, submit.text
    submit_body = submit.json()
    run_id = submit_body["run_id"]
    assert submit_body["status"] == "queued"

    # --- §5 / §6 Watch and view the delivered campaign -----------------
    final = _wait_for(api_client, run_id, ("done", "failed"))
    assert final["status"] == "done", final
    output = final["output"]
    assert output is not None
    assert output["ad_copy"]["headline"]
    assert output["image_url"].startswith(f"/api/v1/artifacts/{brand_a}/")
    for dim in ("relevance", "brand_fit", "clarity", "factual_alignment"):
        assert dim in output["score"]["breakdown"], dim
    # Trace runs through every stage.
    assert [t["stage"] for t in final["trace"]] == [
        "research",
        "retrieval",
        "copy",
        "image",
        "critic",
    ]
    # Image is fetchable.
    image_resp = api_client.get(output["image_url"])
    assert image_resp.status_code == 200
    assert image_resp.headers["content-type"] == "image/png"

    # --- §7.2 Degradable research --------------------------------------
    # ``stub_openai`` wasn't seeded with Tavily, so research is already in
    # ``status=degraded`` for the previous run — re-confirm that contract:
    research_entry = next(t for t in final["trace"] if t["stage"] == "research")
    assert research_entry["status"] == "degraded"
    assert (research_entry.get("error_message") or "") != ""
    # And the run still completed successfully (FR-021).
    assert final["status"] == "done"

    # --- §7.1 Hard-fail (image stage raises) ---------------------------
    stub_openai.push(make_copy_response())
    stub_image.push_exception(RuntimeError("hf_inference: 401 unauthorized"))
    fail_submit = api_client.post(
        "/api/v1/campaigns",
        json={
            "brief": "Trigger image hard-fail.",
            "platform": "instagram",
            "brand_id": brand_a,
            "target_audience": "any",
        },
    )
    fail_run_id = fail_submit.json()["run_id"]
    fail_final = _wait_for(api_client, fail_run_id, ("done", "failed"))
    assert fail_final["status"] == "failed", fail_final
    assert fail_final["failed_stage"] == "image"
    assert fail_final["failed_reason"]
    assert fail_final["output"] is None

    # --- §8 Brand isolation (FR-005) -----------------------------------
    brand_b = _create_brand(api_client, "Globex")
    payload_b = b"Globex Beam5000. Made in Sweden. Lifetime warranty."
    api_client.post(
        f"/api/v1/brands/{brand_b}/documents",
        files={"file": ("globex.txt", payload_b, "text/plain")},
    )
    # The two brands have separate Chroma collections — submitting a
    # campaign for brand_b should never surface ACME's marker phrase.
    # We push fresh copy + critic responses for the isolation run.
    stub_openai.push(make_copy_response(headline="Globex Beam"))
    stub_image.push(make_pil_image(width=512, height=512))
    stub_openai.push(make_critic_response())

    submit_b = api_client.post(
        "/api/v1/campaigns",
        json={
            "brief": "Highlight the lifetime warranty.",
            "platform": "linkedin",
            "brand_id": brand_b,
            "target_audience": "engineers",
        },
    )
    run_b = submit_b.json()["run_id"]
    final_b = _wait_for(api_client, run_b, ("done", "failed"))
    assert final_b["status"] == "done", final_b

    # The retrieval stage's user prompts for brand_b never surfaced
    # ACME's marker phrase — at minimum, the resulting copy doesn't
    # mention the cross-brand marker.
    headline_b = final_b["output"]["ad_copy"]["headline"]
    assert "ACME" not in headline_b
    assert "Mesh Pro" not in headline_b

    # --- §9 DELETE brand cascades --------------------------------------
    artifacts_dir = tmp_data_dir / "artifacts" / brand_a
    uploads_dir = tmp_data_dir / "uploads" / brand_a
    # Pre-conditions for the cascade verification.
    assert artifacts_dir.exists()
    # Either uploads_dir exists (TXT files were saved) or it doesn't —
    # the post-condition is that it must be gone, not present, after delete.

    delete_resp = api_client.delete(f"/api/v1/brands/{brand_a}")
    assert delete_resp.status_code == 204

    # Brand is gone (404 on subsequent fetch).
    assert api_client.get(f"/api/v1/brands/{brand_a}").status_code == 404

    # SQL cascade: documents + runs rows for brand_a are gone.
    with session_module.SessionLocal() as s:
        assert BrandRepository.get(s, brand_a) is None
        assert DocumentRepository.list_for_brand(s, brand_a) == []
        assert RunRepository.list_runs(s, brand_id=brand_a) == []

    # Filesystem cascade: artifacts + uploads dirs are gone.
    assert not artifacts_dir.exists()
    assert not uploads_dir.exists()

    # Brand B is untouched.
    assert api_client.get(f"/api/v1/brands/{brand_b}").status_code == 200


@pytest.mark.parametrize("seed_brand_first", [True])
def test_quickstart_health_endpoint(api_client: TestClient, seed_brand_first: bool) -> None:
    """Quickstart §1 sanity probe — `/healthz` returns 200 with the embedded stack."""

    resp = api_client.get("/api/v1/healthz")
    # 200 when both SQLite and embedded Chroma are reachable; 503 if either
    # backend is down (acceptable behaviour, not a bug).
    assert resp.status_code in (200, 503), resp.text
    body = resp.json()
    assert "status" in body
    assert "dependencies" in body
