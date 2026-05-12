"""Critic stage — OpenRouter structured-output verdict.

Returns a :class:`CriticScore` whose ``breakdown`` MUST contain at minimum
the four required dimensions (relevance, brand_fit, clarity,
factual_alignment). Pass/fail is ``overall >= threshold``. Hard-fails on
malformed model output (FR-021).
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
    CriticScore,
    GeneratedImage,
    ModelCall,
    RetrievedContext,
)
from agents.stages import StageError
from agents.stages.copy import _make_llm_client

logger = logging.getLogger("aura.stages.critic")

REQUIRED_DIMENSIONS: tuple[str, ...] = (
    "relevance",
    "brand_fit",
    "clarity",
    "factual_alignment",
)


def _model_name() -> str:
    return os.getenv("AURA_LLM_MODEL", "gpt-4o-mini")


def _build_prompt(
    ad_copy: AdCopy,
    image_meta: GeneratedImage,
    retrieved: RetrievedContext,
    request: CampaignRequest,
) -> tuple[str, str]:
    chunk_excerpts = "\n".join(f"- {c.text[:300]}" for c in retrieved.chunks[:5]) or "- (none)"
    research_note = (
        "Research signal was unavailable for this run; do not penalise missing market context."
        if not retrieved.chunks and not retrieved.brand_voice
        else ""
    )

    system = (
        "You are an expert marketing critic. Score the ad on four dimensions "
        "[0, 1] each: relevance, brand_fit, clarity, factual_alignment. "
        "Compute overall as the equal-weighted mean. The pass threshold is "
        f"{os.getenv('AURA_CRITIC_THRESHOLD', '0.7')}. Return strictly JSON:\n"
        '{ "overall": float, "breakdown": { "relevance": float, "brand_fit": float, '
        '"clarity": float, "factual_alignment": float }, '
        '"feedback": str, "passed": bool }'
    )

    user = (
        f"Platform: {request.platform}\n"
        f"Audience: {request.target_audience}\n"
        f"Brief: {request.brief}\n\n"
        f"Brand document excerpts (factual_alignment baseline):\n{chunk_excerpts}\n\n"
        f"Generated copy:\n"
        f"  Headline: {ad_copy.headline}\n"
        f"  Primary text: {ad_copy.primary_text}\n"
        f"  CTA: {ad_copy.cta}\n\n"
        f"Image prompt used: {image_meta.prompt}\n"
        f"{research_note}\n"
        f"Score this ad and return JSON only."
    )

    return system, user


def _parse_breakdown(payload: dict[str, Any]) -> dict[str, float]:
    breakdown = payload.get("breakdown")
    if not isinstance(breakdown, dict):
        raise StageError("openrouter critic: breakdown is not an object")
    missing = [dim for dim in REQUIRED_DIMENSIONS if dim not in breakdown]
    if missing:
        raise StageError(f"openrouter critic: breakdown missing required dimensions: {missing}")
    out: dict[str, float] = {}
    for dim, value in breakdown.items():
        if not isinstance(value, (int, float)):
            raise StageError(f"openrouter critic: breakdown[{dim}] not a number")
        if not 0.0 <= float(value) <= 1.0:
            raise StageError(f"openrouter critic: breakdown[{dim}] outside [0, 1]")
        out[dim] = float(value)
    return out


def _build_score(payload: dict[str, Any], threshold: float) -> CriticScore:
    breakdown = _parse_breakdown(payload)

    overall_raw = payload.get("overall")
    if not isinstance(overall_raw, (int, float)):
        raise StageError("openrouter critic: overall is not a number")
    overall = float(overall_raw)
    if not 0.0 <= overall <= 1.0:
        raise StageError(f"openrouter critic: overall {overall} outside [0, 1]")

    feedback = payload.get("feedback", "")
    if not isinstance(feedback, str):
        raise StageError("openrouter critic: feedback is not a string")

    passed = overall >= threshold

    return CriticScore(
        overall=overall,
        breakdown=breakdown,
        feedback=feedback,
        passed=passed,
    )


def run_critic(
    ad_copy: AdCopy,
    image_meta: GeneratedImage,
    retrieved: RetrievedContext,
    request: CampaignRequest,
    *,
    threshold: float,
) -> tuple[CriticScore, list[ModelCall]]:
    """Run the critic stage. Hard-fails on malformed output."""

    system, user = _build_prompt(ad_copy, image_meta, retrieved, request)
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
            temperature=0.0,
        )
    except StageError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise StageError(f"openrouter critic: API call failed: {exc}") from exc

    latency_ms = max(0, int((time.monotonic() - started_at) * 1000))

    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError) as exc:
        raise StageError(f"openrouter critic: response shape unexpected: {exc}") from exc

    if not isinstance(content, str) or not content:
        raise StageError("openrouter critic: response content was empty")

    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise StageError(f"openrouter critic: response was not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise StageError("openrouter critic: response JSON was not an object")

    score = _build_score(payload, threshold)

    usage = getattr(response, "usage", None)
    token_in = getattr(usage, "prompt_tokens", None) if usage is not None else None
    token_out = getattr(usage, "completion_tokens", None) if usage is not None else None

    return score, [
        ModelCall(
            provider="openrouter",
            model=model,
            op="chat.completions",
            latency_ms=latency_ms,
            token_in=token_in,
            token_out=token_out,
        )
    ]


__all__ = ["REQUIRED_DIMENSIONS", "run_critic"]
