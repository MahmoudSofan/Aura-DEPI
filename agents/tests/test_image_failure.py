"""FR-021 hard-fail image path + SC-005 30 s budget."""

from __future__ import annotations

import time

import pytest
from agents.schemas import AdCopy, CampaignRequest
from agents.stages import StageError
from agents.stages.image import run_image
from backend.tests.conftest import (
    StubRecorder,
    make_copy_response,
)
from fastapi.testclient import TestClient


def test_image_raises_stage_error_on_provider_failure(
    stub_image: StubRecorder, tmp_path: pytest.TempPathFactory
) -> None:
    stub_image.push_exception(RuntimeError("401 unauthorized"))
    output_path = tmp_path / "01HXFAKE.png"  # type: ignore[operator]
    ad_copy = AdCopy(
        headline="h",
        primary_text="p",
        cta="c",
        platform="instagram",
    )
    request = CampaignRequest(
        brief="b", platform="instagram", brand_id="01HXBRAND", target_audience="ya"
    )
    with pytest.raises(StageError) as excinfo:
        run_image(ad_copy, request, output_path=output_path)
    assert "openrouter image" in str(excinfo.value)
    assert "401" in str(excinfo.value)


def test_image_failure_terminates_run_within_sc005_budget(
    api_client: TestClient,
    seeded_brand_id_via_client: str,
    stub_openai: StubRecorder,
    stub_image: StubRecorder,
) -> None:
    """SC-005: image hard-fail surfaces within 30 s; status='failed', no output."""

    stub_openai.push(make_copy_response())
    stub_image.push_exception(RuntimeError("simulated 401 unauthorized"))

    started = time.monotonic()
    submit = api_client.post(
        "/api/v1/campaigns",
        json={
            "brief": "Brief",
            "platform": "instagram",
            "brand_id": seeded_brand_id_via_client,
            "target_audience": "ya",
        },
    )
    run_id = submit.json()["run_id"]

    deadline = time.monotonic() + 30.0
    final = None
    while time.monotonic() < deadline:
        body = api_client.get(f"/api/v1/campaigns/{run_id}").json()
        if body["status"] == "failed":
            final = body
            break
        time.sleep(0.05)

    elapsed = time.monotonic() - started
    assert final is not None, "run never reached failed within 30s"
    assert elapsed < 30.0, f"hard-fail surfaced in {elapsed:.2f}s (>30s budget)"
    assert final["failed_stage"] == "image"
    assert final["failed_reason"] is not None
    assert final["output"] is None
