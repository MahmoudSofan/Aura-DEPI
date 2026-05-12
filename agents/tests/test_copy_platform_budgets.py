"""Copy stage — per-platform prompt budgets and structurally-valid output."""

from __future__ import annotations

import pytest
from agents.schemas import CampaignRequest, Platform, ResearchOutput, RetrievedContext
from agents.stages.copy import PLATFORM_BUDGETS, build_prompt, run_copy
from backend.tests.conftest import StubRecorder, make_copy_response

SUPPORTED_PLATFORMS: tuple[Platform, ...] = (
    "facebook",
    "instagram",
    "tiktok",
    "twitter",
    "linkedin",
    "youtube",
)


@pytest.mark.parametrize("platform", SUPPORTED_PLATFORMS)
def test_prompt_includes_platform_budget(platform: Platform) -> None:
    request = CampaignRequest(
        brief="b", platform=platform, brand_id="01HXBRAND", target_audience="ya"
    )
    research = ResearchOutput(trends=[], competitors=[], sources=[])
    retrieved = RetrievedContext(chunks=[], brand_voice="")
    system, _user = build_prompt(request, research, retrieved)

    budget = PLATFORM_BUDGETS[platform]
    assert budget["headline"] in system
    assert budget["primary_text"] in system
    assert budget["cta"] in system
    assert platform in system


@pytest.mark.parametrize("platform", SUPPORTED_PLATFORMS)
def test_run_copy_returns_valid_adcopy(platform: Platform, stub_openai: StubRecorder) -> None:
    stub_openai.push(
        make_copy_response(
            headline=f"H for {platform}",
            primary_text=f"Body for {platform}",
            cta="Tap",
            platform=platform,
        )
    )
    request = CampaignRequest(
        brief="b", platform=platform, brand_id="01HXBRAND", target_audience="ya"
    )
    research = ResearchOutput(trends=[], competitors=[], sources=[])
    retrieved = RetrievedContext(chunks=[], brand_voice="")
    ad_copy, calls = run_copy(request, research, retrieved)
    assert ad_copy.platform == platform
    assert ad_copy.headline.startswith("H for")
    assert len(calls) == 1
    assert calls[0].provider == "openrouter"


def test_prior_critic_feedback_appears_in_user_prompt() -> None:
    request = CampaignRequest(
        brief="b", platform="instagram", brand_id="01HXBRAND", target_audience="ya"
    )
    research = ResearchOutput(trends=[], competitors=[], sources=[])
    retrieved = RetrievedContext(chunks=[], brand_voice="")
    _system, user = build_prompt(
        request,
        research,
        retrieved,
        prior_critic_feedback="too generic — add brand specifics",
    )
    assert "too generic" in user
    assert "Previous attempt feedback" in user
