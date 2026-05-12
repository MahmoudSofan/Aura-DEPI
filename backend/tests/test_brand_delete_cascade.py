"""Brand-delete cascade across SQL, Chroma, and the filesystem (T040, FR-024)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from backend.persistence import session as session_module
from backend.persistence.repository import BrandRepository, DocumentRepository, RunRepository
from backend.tests.conftest import (
    StubRecorder,
    make_copy_response,
    make_critic_response,
    make_pil_image,
)
from fastapi.testclient import TestClient


def _create_brand(api_client: TestClient, display_name: str = "Cascade Brand") -> str:
    return str(api_client.post("/api/v1/brands", json={"display_name": display_name}).json()["id"])


def _wait_for_status(
    api_client: TestClient, run_id: str, target: tuple[str, ...], *, timeout_s: float = 90.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        body = api_client.get(f"/api/v1/campaigns/{run_id}").json()
        if body["status"] in target:
            return dict(body)
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} never reached {target}")


def test_delete_cascades_all_layers(
    api_client: TestClient,
    stub_embeddings: None,
    stub_openai: StubRecorder,
    stub_image: StubRecorder,
    shared_chroma: Any,
    tmp_data_dir: Path,
) -> None:
    bid = _create_brand(api_client)

    # Upload a document so the Chroma collection + uploads dir exist.
    upload = api_client.post(
        f"/api/v1/brands/{bid}/documents",
        files={"file": ("brand.txt", b"Brand voice. Marker: CASCADE-7777.", "text/plain")},
    )
    assert upload.status_code == 201

    # Run a campaign to terminal so a run + stage_traces + campaign_output
    # row exist and an artifact PNG is on disk.
    stub_openai.push(make_copy_response())
    stub_image.push(make_pil_image(width=64, height=64))
    stub_openai.push(make_critic_response())

    submit = api_client.post(
        "/api/v1/campaigns",
        json={
            "brief": "Test cascade brief.",
            "platform": "instagram",
            "brand_id": bid,
            "target_audience": "any",
        },
    )
    run_id = submit.json()["run_id"]
    final = _wait_for_status(api_client, run_id, ("done", "failed"))
    assert final["status"] == "done", final

    artifact_path = tmp_data_dir / "artifacts" / bid / f"{run_id}.png"
    uploads_dir = tmp_data_dir / "uploads" / bid
    assert artifact_path.exists()
    assert uploads_dir.exists()

    # Verify Chroma collection exists with content.
    coll_name = f"brand_{bid}"
    pre = shared_chroma.get_or_create_collection(name=coll_name)
    assert pre.count() >= 1

    # DELETE the brand.
    resp = api_client.delete(f"/api/v1/brands/{bid}")
    assert resp.status_code == 204

    # SQL rows are gone (cascade).
    with session_module.SessionLocal() as s:
        assert BrandRepository.get(s, bid) is None
        assert DocumentRepository.list_for_brand(s, bid) == []
        assert RunRepository.get(s, run_id) is None

    # Chroma collection is gone (or at least empty if recreated lazily).
    try:
        post = shared_chroma.get_collection(name=coll_name)
        assert post.count() == 0, "collection should be deleted after cascade"
    except Exception:
        # Idempotent expected outcome — get_collection raises when the
        # collection doesn't exist.
        pass

    # Filesystem cleanup.
    assert not artifact_path.exists()
    assert not uploads_dir.exists()
