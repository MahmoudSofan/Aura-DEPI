"""Copywriter stage — OpenRouter (OpenAI-compatible) structured output.

Per-platform character budgets are guidance in the system prompt (not
post-hoc validators) per research.md §12. Hard-fails on OpenRouter
errors or malformed model output (FR-021).
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from agents.schemas import (
    AdCopy,
    CampaignRequest,
    ModelCall,
    Platform,
    ResearchOutput,
    RetrievedContext,
)
from agents.stages import StageError

logger = logging.getLogger("aura.stages.copy")


# research.md §12 — guidance budgets, soft enforcement via critic clarity dim.
PLATFORM_BUDGETS: dict[Platform, dict[str, str]] = {
    "facebook": {"headline": "≤ 40 chars", "primary_text": "≤ 125 chars", "cta": "≤ 20 chars"},
    "instagram": {"headline": "≤ 40 chars", "primary_text": "≤ 220 chars", "cta": "≤ 20 chars"},
    "tiktok": {"headline": "≤ 40 chars", "primary_text": "≤ 150 chars", "cta": "≤ 20 chars"},
    "twitter": {
        "headline": "≤ 50 chars",
        "primary_text": "≤ 280 chars total (headline + primary text + CTA combined)",
        "cta": "≤ 20 chars",
    },
    "linkedin": {"headline": "≤ 60 chars", "primary_text": "≤ 700 chars", "cta": "≤ 25 chars"},
    "youtube": {
        "headline": "≤ 70 chars (title)",
        "primary_text": "≤ 1500 chars (description)",
        "cta": "≤ 25 chars",
    },
}


_DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def _make_llm_client() -> Any:
    """Construct the OpenAI-compatible client pointed at OpenRouter.

    Both copy and critic share this factory. Tests monkey-patch the binding
    on each module (critic imports it by name from copy).
    """

    from openai import OpenAI

    api_key = os.getenv("OPENROUTER_API_KEY")
    base_url = os.getenv("OPENROUTER_BASE_URL") or _DEFAULT_OPENROUTER_BASE_URL
    if not api_key:
        raise StageError("openrouter: OPENROUTER_API_KEY not set")
    return OpenAI(api_key=api_key, base_url=base_url)


def _model_name() -> str:
    return os.getenv("AURA_LLM_MODEL", "openai/gpt-4o-mini")


def build_prompt(
    request: CampaignRequest,
    research: ResearchOutput,
    retrieved: RetrievedContext,
    *,
    prior_critic_feedback: str | None = None,
) -> tuple[str, str]:
    """Return ``(system_prompt, user_prompt)`` for the copywriter call.

    The split lets tests assert prompt structure without parsing a single
    glued string.
    """

    budget = PLATFORM_BUDGETS[request.platform]
    chunk_lines = (
        "\n".join(f"- {c.text}" for c in retrieved.chunks[:5]) or "- (no brand documents indexed)"
    )
    competitors_line = ", ".join(research.competitors) or "(none)"
    trends_line = "; ".join(research.trends) or "(none)"

    system = (
        "You are a senior performance-marketing copywriter. Generate a single ad in JSON.\n"
        f"Platform: {request.platform}.\n"
        f"Length budgets — headline {budget['headline']}, "
        f"primary text {budget['primary_text']}, CTA {budget['cta']}.\n"
        "Tone is platform-appropriate (concise on Twitter, professional on LinkedIn,\n"
        "conversational on TikTok, etc.). Lean into facts the brand has already published\n"
        "rather than inventing. Output strictly the following JSON schema and nothing else:\n"
        '{ "headline": str, "primary_text": str, "cta": str, "platform": str }'
    )

    feedback_section = (
        f"\nPrevious attempt feedback (incorporate, do not repeat the previous mistakes):\n{prior_critic_feedback}\n"
        if prior_critic_feedback
        else ""
    )

    user = (
        f"Brand id: {request.brand_id}\n"
        f"Audience: {request.target_audience}\n"
        f"Brief: {request.brief}\n\n"
        f"Brand voice (synthesised from indexed brand documents):\n{retrieved.brand_voice or '(empty)'}\n\n"
        f"Brand document excerpts (use specific facts, names, prices verbatim where applicable):\n{chunk_lines}\n\n"
        f"Recent market signal — competitors: {competitors_line}; trends: {trends_line}.\n"
        f"{feedback_section}"
        f"Return JSON only."
    )

    return system, user


def _parse_response(content: str, platform: Platform) -> AdCopy:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise StageError(f"openrouter copy: response was not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise StageError("openrouter copy: response JSON was not an object")

    payload.setdefault("platform", platform)
    try:
        return AdCopy(**payload)
    except Exception as exc:  # noqa: BLE001
        raise StageError(f"openrouter copy: malformed AdCopy: {exc}") from exc


def run_copy(
    request: CampaignRequest,
    research: ResearchOutput,
    retrieved: RetrievedContext,
    *,
    prior_critic_feedback: str | None = None,
) -> tuple[AdCopy, list[ModelCall]]:
    """Run the copywriter stage. Hard-fails on OpenRouter errors."""

    system, user = build_prompt(
        request,
        research,
        retrieved,
        prior_critic_feedback=prior_critic_feedback,
    )
    model = _model_name()

    started_at = time.monotonic()
    try:
        client = _make_llm_client()
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            temperature=0.7,
        )
    except StageError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise StageError(f"openrouter copy: API call failed: {exc}") from exc

    latency_ms = max(0, int((time.monotonic() - started_at) * 1000))

    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError) as exc:
        raise StageError(f"openrouter copy: response shape unexpected: {exc}") from exc

    if not isinstance(content, str) or not content:
        raise StageError("openrouter copy: response content was empty")

    ad_copy = _parse_response(content, request.platform)

    usage = getattr(response, "usage", None)
    token_in = getattr(usage, "prompt_tokens", None) if usage is not None else None
    token_out = getattr(usage, "completion_tokens", None) if usage is not None else None

    return ad_copy, [
        ModelCall(
            provider="openrouter",
            model=model,
            op="chat.completions",
            latency_ms=latency_ms,
            token_in=token_in,
            token_out=token_out,
        )
    ]


__all__ = ["PLATFORM_BUDGETS", "build_prompt", "run_copy"]
