"""Critic stage unit tests."""

from __future__ import annotations

import json

import pytest
from agents.schemas import (
    AdCopy,
    CampaignRequest,
    GeneratedImage,
    RetrievedContext,
)
from agents.stages import StageError
from agents.stages.critic import REQUIRED_DIMENSIONS, run_critic
from backend.tests.conftest import StubRecorder, make_critic_response


def _request() -> CampaignRequest:
    return CampaignRequest(
        brief="b", platform="instagram", brand_id="01HXBRAND", target_audience="ya"
    )


def _ad_copy() -> AdCopy:
    return AdCopy(headline="h", primary_text="p", cta="c", platform="instagram")


def _image() -> GeneratedImage:
    return GeneratedImage(
        path="/api/v1/artifacts/01HXBRAND/01HXFAKE.png",
        prompt="prompt",
        negative_prompt="neg",
        dimensions=(1024, 1024),
    )


def _retrieved() -> RetrievedContext:
    return RetrievedContext(chunks=[], brand_voice="")


def test_critic_returns_score_with_all_dimensions(stub_openai: StubRecorder) -> None:
    stub_openai.push(make_critic_response(overall=0.85))
    score, calls = run_critic(_ad_copy(), _image(), _retrieved(), _request(), threshold=0.7)
    for dim in REQUIRED_DIMENSIONS:
        assert dim in score.breakdown
        assert 0.0 <= score.breakdown[dim] <= 1.0
    assert score.overall == 0.85
    assert score.passed is True
    assert len(calls) == 1
    assert calls[0].provider == "openrouter"


def test_critic_passed_flag_uses_threshold(stub_openai: StubRecorder) -> None:
    stub_openai.push(make_critic_response(overall=0.65))
    score, _ = run_critic(_ad_copy(), _image(), _retrieved(), _request(), threshold=0.7)
    assert score.overall == 0.65
    assert score.passed is False


def test_critic_rejects_missing_dimensions(stub_openai: StubRecorder) -> None:
    bad = json.dumps(
        {
            "overall": 0.8,
            "breakdown": {
                "relevance": 0.8,
                "brand_fit": 0.8,
                "clarity": 0.8,
            },  # missing factual_alignment
            "feedback": "ok",
            "passed": True,
        }
    )
    stub_openai.push(bad)
    with pytest.raises(StageError, match="missing required dimensions"):
        run_critic(_ad_copy(), _image(), _retrieved(), _request(), threshold=0.7)


def test_critic_rejects_invalid_overall(stub_openai: StubRecorder) -> None:
    bad = json.dumps(
        {
            "overall": 1.5,
            "breakdown": {
                "relevance": 0.8,
                "brand_fit": 0.8,
                "clarity": 0.8,
                "factual_alignment": 0.8,
            },
            "feedback": "ok",
            "passed": True,
        }
    )
    stub_openai.push(bad)
    with pytest.raises(StageError, match="overall"):
        run_critic(_ad_copy(), _image(), _retrieved(), _request(), threshold=0.7)


def test_critic_rejects_non_json(stub_openai: StubRecorder) -> None:
    stub_openai.push("not even close to JSON")
    with pytest.raises(StageError, match="JSON"):
        run_critic(_ad_copy(), _image(), _retrieved(), _request(), threshold=0.7)
