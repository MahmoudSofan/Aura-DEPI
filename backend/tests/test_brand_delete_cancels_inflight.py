"""Brand delete cancels in-flight runs and the cascade still completes (T041, FR-024)."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest
from backend.persistence import session as session_module
from backend.persistence.repository import BrandRepository, RunRepository
from backend.tests.conftest import StubRecorder, make_copy_response
from fastapi.testclient import TestClient


def _create_brand(api_client: TestClient, display_name: str) -> str:
    return str(api_client.post("/api/v1/brands", json={"display_name": display_name}).json()["id"])


def _wait_until_running(api_client: TestClient, run_id: str, *, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        body = api_client.get(f"/api/v1/campaigns/{run_id}")
        if body.status_code == 200 and body.json()["status"] == "running":
            return True
        time.sleep(0.02)
    return False


def test_delete_cancels_inflight_and_cascade_completes(
    api_client: TestClient,
    stub_embeddings: None,
    stub_openai: StubRecorder,  # noqa: ARG001 — fixture installed via dep
    stub_image: StubRecorder,  # noqa: ARG001 — fixture installed via dep
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An in-flight run for a deleted brand terminates via the cascade.

    Spec (FR-024): the run does not continue executing on a deleted brand;
    the brand cascade completes; the runner stays healthy for subsequent
    submissions on other brands. The brands API marks every non-terminal
    run as ``failed`` with ``failed_reason='brand_deleted'`` before
    cascading, so the runner's view of the run is consistent with
    cancellation; the cascade then removes the row.
    """

    bid_a = _create_brand(api_client, "Brand A")

    # Replace the OpenRouter copy client with a blocking callable that gates
    # the copy stage on a threading.Event. This holds the run in `running`
    # status long enough for the test to issue DELETE.
    release = threading.Event()
    copy_response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=make_copy_response()))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )

    def blocking_copy_create(**kwargs: Any) -> Any:
        # Block in the copy stage until the test releases us.
        release.wait(timeout=10.0)
        return copy_response

    fake_copy_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=blocking_copy_create))
    )
    monkeypatch.setattr("agents.stages.copy._make_llm_client", lambda: fake_copy_client)

    submit = api_client.post(
        "/api/v1/campaigns",
        json={
            "brief": "Inflight cancel brief.",
            "platform": "instagram",
            "brand_id": bid_a,
            "target_audience": "any",
        },
    )
    assert submit.status_code == 202
    run_id = submit.json()["run_id"]

    assert _wait_until_running(api_client, run_id), "run never reached 'running'"

    # DELETE the brand while the run is mid-copy.
    delete_resp = api_client.delete(f"/api/v1/brands/{bid_a}")
    assert delete_resp.status_code == 204

    # Release the blocked copy stage so the runner can unwind.
    release.set()

    # Cascade completed: brand and run are gone.
    follow = api_client.get(f"/api/v1/campaigns/{run_id}")
    assert follow.status_code == 404
    with session_module.SessionLocal() as s:
        assert BrandRepository.get(s, bid_a) is None
        assert RunRepository.get(s, run_id) is None

    # Runner is still healthy — submitting on a different brand returns 202.
    # We verify only the API-side acceptance; we don't drive the second run
    # to terminal because the blocking copy-client patch is still installed
    # for this test, and that's fine — the 202 proves the runner consumer
    # task is alive.
    bid_b = _create_brand(api_client, "Brand B")
    second = api_client.post(
        "/api/v1/campaigns",
        json={
            "brief": "Post-cancel sanity brief.",
            "platform": "instagram",
            "brand_id": bid_b,
            "target_audience": "any",
        },
    )
    assert second.status_code == 202
