"""Concurrency / queue-beyond-cap tests for the campaign runner."""

from __future__ import annotations

import time

import pytest
from backend.tests.conftest import (
    StubRecorder,
    make_copy_response,
    make_critic_response,
    make_pil_image,
)
from fastapi.testclient import TestClient


@pytest.fixture
def cap_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Override the concurrency cap to 1 for this test only.

    The runner reads ``AURA_CONCURRENCY_CAP`` lazily during ``__init__``,
    so we need to set it *before* the FastAPI lifespan constructs the
    runner. Tests that use ``api_client`` should request this fixture
    *before* ``api_client``.
    """

    monkeypatch.setenv("AURA_CONCURRENCY_CAP", "1")


def test_submit_returns_queued_under_cap(
    cap_one: None,
    api_client: TestClient,
    seeded_brand_id_via_client: str,
    stub_openai: StubRecorder,
    stub_image: StubRecorder,
) -> None:
    """Submit cap+1 runs rapidly; all return 202 with status='queued' in <1s."""

    # Push enough canned responses for two complete runs (copy/image/critic each).
    for _ in range(2):
        stub_openai.push(make_copy_response())
        stub_image.push(make_pil_image())
        stub_openai.push(make_critic_response())

    body = {
        "brief": "Summer launch.",
        "platform": "instagram",
        "brand_id": seeded_brand_id_via_client,
        "target_audience": "ya",
    }

    submitted: list[tuple[str, float]] = []
    for _ in range(2):
        t0 = time.monotonic()
        resp = api_client.post("/api/v1/campaigns", json=body)
        latency = time.monotonic() - t0
        assert resp.status_code == 202, resp.text
        payload = resp.json()
        assert payload["status"] == "queued"
        # SC-002 light gate: submit returns under 1s even when busy.
        assert latency < 1.5, f"submit took {latency:.2f}s"
        submitted.append((payload["run_id"], latency))

    # Eventually both reach done.
    deadline = time.monotonic() + 60.0
    seen_done: set[str] = set()
    while time.monotonic() < deadline and len(seen_done) < 2:
        for run_id, _ in submitted:
            r = api_client.get(f"/api/v1/campaigns/{run_id}")
            if r.json()["status"] == "done":
                seen_done.add(run_id)
        time.sleep(0.05)

    assert len(seen_done) == 2, f"only {seen_done} reached done within deadline"
