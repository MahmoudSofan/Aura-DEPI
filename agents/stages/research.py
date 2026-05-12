"""Research stage — Tavily web search.

Degradable per FR-021 (research is the *only* degradable stage in the
pipeline). On timeout, missing API key, or any Tavily error the function
returns an empty :class:`ResearchOutput` plus ``status='degraded'`` and a
human-readable reason; it never raises.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from agents.schemas import CampaignRequest, ModelCall, ResearchOutput, StageStatus

logger = logging.getLogger("aura.stages.research")

_DEFAULT_TIMEOUT_S = 10
_MAX_RESULTS = 5


def _make_tavily_client() -> Any:
    """Construct a Tavily client. Patched in tests via monkey-patch."""

    from tavily import TavilyClient

    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        raise RuntimeError("TAVILY_API_KEY not set")
    return TavilyClient(api_key=api_key)


def _empty() -> ResearchOutput:
    return ResearchOutput(trends=[], competitors=[], sources=[])


def _build_query(request: CampaignRequest) -> str:
    return (
        f"Marketing trends and competitors for {request.target_audience}. "
        f"Brief: {request.brief[:200]}"
    )


def run_research(
    request: CampaignRequest,
    *,
    deadline_s: int = _DEFAULT_TIMEOUT_S,
) -> tuple[ResearchOutput, list[ModelCall], StageStatus, str | None]:
    """Run the research stage. Never raises.

    Returns ``(output, model_calls, status, error_message)``. ``status`` is
    ``"ok"`` or ``"degraded"``; ``error_message`` is non-None iff degraded.
    """

    if not os.getenv("TAVILY_API_KEY"):
        reason = "tavily: TAVILY_API_KEY not set; research skipped"
        logger.info(reason)
        return _empty(), [], "degraded", reason

    started_at = time.monotonic()
    try:
        client = _make_tavily_client()
        query = _build_query(request)
        result = client.search(
            query=query,
            max_results=_MAX_RESULTS,
            search_depth="basic",
            timeout=deadline_s,
        )
    except Exception as exc:  # noqa: BLE001 — degradable: any failure ⇒ skip
        latency_ms = max(0, int((time.monotonic() - started_at) * 1000))
        reason = f"tavily: {exc}"
        logger.warning("research stage degraded: %s", reason)
        return (
            _empty(),
            [
                ModelCall(
                    provider="tavily",
                    model="search",
                    op="search",
                    latency_ms=latency_ms,
                )
            ],
            "degraded",
            reason,
        )

    latency_ms = max(0, int((time.monotonic() - started_at) * 1000))
    items = result.get("results", []) if isinstance(result, dict) else []
    competitors = [item.get("title", "") for item in items[:3] if isinstance(item, dict)]
    sources = [item.get("url", "") for item in items if isinstance(item, dict) if item.get("url")]
    answer = result.get("answer") if isinstance(result, dict) else None
    trends = [answer] if isinstance(answer, str) and answer else []

    return (
        ResearchOutput(
            trends=[t for t in trends if t],
            competitors=[c for c in competitors if c],
            sources=sources,
        ),
        [
            ModelCall(
                provider="tavily",
                model="search",
                op="search",
                latency_ms=latency_ms,
            )
        ],
        "ok",
        None,
    )


__all__ = ["run_research"]
