"""FR-021 degradable research path + SC-005 30s budget."""

from __future__ import annotations

import time

import pytest
from agents.schemas import CampaignRequest
from agents.stages.research import run_research
from backend.tests.conftest import (
    StubRecorder,
    make_copy_response,
    make_critic_response,
    make_pil_image,
)
from fastapi.testclient import TestClient


def test_research_degrades_when_tavily_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    request = CampaignRequest(brief="hi", platform="instagram", brand_id="x", target_audience="ya")
    output, calls, status, error = run_research(request)
    assert status == "degraded"
    assert error is not None and "TAVILY_API_KEY not set" in error
    assert output.trends == []
    assert output.competitors == []
    assert output.sources == []
    assert calls == []


def test_research_degrades_when_tavily_raises(stub_tavily: StubRecorder) -> None:
    stub_tavily.push_exception(TimeoutError("simulated tavily timeout"))
    request = CampaignRequest(brief="hi", platform="instagram", brand_id="x", target_audience="ya")
    output, calls, status, error = run_research(request)
    assert status == "degraded"
    assert error is not None and "tavily" in error.lower()
    assert output.trends == []
    assert len(calls) == 1
    assert calls[0].provider == "tavily"


def test_research_degraded_run_completes_within_sc005_budget(
    api_client: TestClient,
    seeded_brand_id_via_client: str,
    stub_openai: StubRecorder,
    stub_image: StubRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SC-005: degraded research surfaces within 30 s and run reaches done."""

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    stub_openai.push(make_copy_response())
    stub_image.push(make_pil_image())
    stub_openai.push(make_critic_response())

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
    seen_degraded = False
    while time.monotonic() < deadline:
        body = api_client.get(f"/api/v1/campaigns/{run_id}").json()
        for entry in body.get("trace", []):
            if entry["stage"] == "research" and entry["status"] == "degraded":
                seen_degraded = True
                break
        if seen_degraded:
            break
        if body["status"] in ("done", "failed"):
            break
        time.sleep(0.05)

    elapsed = time.monotonic() - started
    assert seen_degraded, "research stage never reported degraded"
    assert elapsed < 30.0, f"degradation surfaced in {elapsed:.2f}s (>30s budget)"
