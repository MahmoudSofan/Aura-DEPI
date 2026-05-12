"""LangGraph state machine for the Aura pipeline.

Topology (US1 baseline + US3 critic-driven retry edge):

```
START ──┬─► research ──┐
        │              ├─► copy ─► image ─► critic ──┬─► END        (pass / cap reached)
        └─► retrieval ─┘     ▲                       │
                             └───────────────────────┘ (retry)
```

Research and retrieval execute in parallel on attempt 1 only (FR-009);
subsequent attempts re-use their outputs. Copy, image and critic run
sequentially per attempt. After the critic returns its verdict the graph
either terminates (verdict passed, or ``attempt >= retry_cap + 1``) or
loops back to copy with the previous critic's feedback in state (FR-015,
FR-016).

The runner picks the highest-scoring attempt across the run; non-winning
attempts' images are deleted at finalisation but their critic scores stay
in ``stage_traces`` for audit.

Per-stage trace entries are surfaced via the ``on_stage_event`` callable
threaded through the graph state so the runner can persist them as they
fire (no buffering across the whole graph).

All hard-fail stage errors propagate as :class:`StageError` with a
``stage`` attribute set so the runner can record ``failed_stage`` /
``failed_reason`` accurately.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

from agents.schemas import (
    AdCopy,
    CampaignRequest,
    CriticScore,
    GeneratedImage,
    ResearchOutput,
    RetrievedContext,
    StageName,
    StageTraceEntry,
)
from agents.stages import StageError
from agents.stages.copy import run_copy
from agents.stages.critic import run_critic
from agents.stages.image import run_image
from agents.stages.research import run_research
from agents.stages.retrieval import run_retrieval
from langgraph.graph import END, START, StateGraph

OnStageEvent = Callable[[StageTraceEntry], Awaitable[None] | None]


@dataclass(frozen=True)
class AttemptResult:
    """One creative+image+critic attempt, recorded by the graph for the runner.

    The runner picks the attempt with the highest ``critic.overall`` as the
    winner, copies its file at ``image_path`` to the canonical artifact path,
    and writes ``campaign_outputs.winning_attempt = attempt``.
    """

    attempt: int
    ad_copy: AdCopy
    image: GeneratedImage
    image_path: Path
    critic: CriticScore


class GraphState(TypedDict, total=False):
    request: CampaignRequest
    run_id: str
    retry_cap: int
    critic_threshold: float
    attempt: int
    artifacts_dir: str
    chroma_client: Any
    on_stage_event: OnStageEvent
    research: ResearchOutput
    retrieved: RetrievedContext
    ad_copy: AdCopy
    image: GeneratedImage
    image_path: str
    critic: CriticScore
    prior_critic_feedback: str | None
    will_retry: bool
    attempts: list[AttemptResult]


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _ms_between(start: datetime, end: datetime) -> int:
    return max(0, int((end - start).total_seconds() * 1000))


async def _emit_event(state: GraphState, entry: StageTraceEntry) -> None:
    cb = state.get("on_stage_event")
    if cb is None:
        return
    result = cb(entry)
    if inspect.isawaitable(result):
        await result


def _attempt(state: GraphState) -> int:
    return int(state.get("attempt") or 1)


def _stage_error(stage: StageName, reason: str) -> StageError:
    err = StageError(reason)
    err.stage = stage  # type: ignore[attr-defined]
    return err


def _attempt_image_path(artifacts_dir: Path, brand_id: str, run_id: str, attempt: int) -> Path:
    """Per-attempt PNG path; the runner promotes the winner to ``{run_id}.png``."""

    return artifacts_dir / brand_id / f"{run_id}.attempt{attempt}.png"


def _canonical_image_url(brand_id: str, run_id: str) -> str:
    return f"/api/v1/artifacts/{brand_id}/{run_id}.png"


# ---------------------------------------------------------------------------
# Nodes.
# ---------------------------------------------------------------------------


async def _research_node(state: GraphState) -> GraphState:
    started = _now()
    output, calls, status, error = await asyncio.to_thread(run_research, state["request"])
    completed = _now()
    entry = StageTraceEntry(
        stage="research",
        attempt=_attempt(state),
        status=status,
        started_at=started,
        completed_at=completed,
        duration_ms=_ms_between(started, completed),
        model_calls=calls,
        verdict=None,
        error_message=error,
    )
    await _emit_event(state, entry)
    return {"research": output}


async def _retrieval_node(state: GraphState) -> GraphState:
    started = _now()
    try:
        output, calls = await asyncio.to_thread(
            run_retrieval, state["request"], chroma_client=state["chroma_client"]
        )
        completed = _now()
        entry = StageTraceEntry(
            stage="retrieval",
            attempt=_attempt(state),
            status="ok",
            started_at=started,
            completed_at=completed,
            duration_ms=_ms_between(started, completed),
            model_calls=calls,
            verdict=None,
            error_message=None,
        )
        await _emit_event(state, entry)
        return {"retrieved": output}
    except StageError as exc:
        completed = _now()
        entry = StageTraceEntry(
            stage="retrieval",
            attempt=_attempt(state),
            status="failed",
            started_at=started,
            completed_at=completed,
            duration_ms=_ms_between(started, completed),
            model_calls=[],
            verdict=None,
            error_message=str(exc),
        )
        await _emit_event(state, entry)
        raise _stage_error("retrieval", str(exc)) from exc


async def _copy_node(state: GraphState) -> GraphState:
    started = _now()
    feedback = state.get("prior_critic_feedback")
    try:
        output, calls = await asyncio.to_thread(
            run_copy,
            state["request"],
            state["research"],
            state["retrieved"],
            prior_critic_feedback=feedback,
        )
        completed = _now()
        entry = StageTraceEntry(
            stage="copy",
            attempt=_attempt(state),
            status="ok",
            started_at=started,
            completed_at=completed,
            duration_ms=_ms_between(started, completed),
            model_calls=calls,
            verdict=None,
            error_message=None,
        )
        await _emit_event(state, entry)
        return {"ad_copy": output}
    except StageError as exc:
        completed = _now()
        entry = StageTraceEntry(
            stage="copy",
            attempt=_attempt(state),
            status="failed",
            started_at=started,
            completed_at=completed,
            duration_ms=_ms_between(started, completed),
            model_calls=[],
            verdict=None,
            error_message=str(exc),
        )
        await _emit_event(state, entry)
        raise _stage_error("copy", str(exc)) from exc


async def _image_node(state: GraphState) -> GraphState:
    started = _now()
    artifacts_dir = Path(state["artifacts_dir"])
    request = state["request"]
    run_id = state["run_id"]
    attempt = _attempt(state)
    output_path = _attempt_image_path(artifacts_dir, request.brand_id, run_id, attempt)
    canonical_url = _canonical_image_url(request.brand_id, run_id)
    feedback = state.get("prior_critic_feedback")
    try:
        output, calls = await asyncio.to_thread(
            run_image,
            state["ad_copy"],
            request,
            output_path=output_path,
            prior_critic_feedback=feedback,
            image_url=canonical_url,
        )
        completed = _now()
        entry = StageTraceEntry(
            stage="image",
            attempt=attempt,
            status="ok",
            started_at=started,
            completed_at=completed,
            duration_ms=_ms_between(started, completed),
            model_calls=calls,
            verdict=None,
            error_message=None,
        )
        await _emit_event(state, entry)
        return {"image": output, "image_path": str(output_path)}
    except StageError as exc:
        completed = _now()
        entry = StageTraceEntry(
            stage="image",
            attempt=attempt,
            status="failed",
            started_at=started,
            completed_at=completed,
            duration_ms=_ms_between(started, completed),
            model_calls=[],
            verdict=None,
            error_message=str(exc),
        )
        await _emit_event(state, entry)
        raise _stage_error("image", str(exc)) from exc


async def _critic_node(state: GraphState) -> GraphState:
    started = _now()
    threshold = float(state["critic_threshold"])
    current_attempt = _attempt(state)
    try:
        verdict, calls = await asyncio.to_thread(
            run_critic,
            state["ad_copy"],
            state["image"],
            state["retrieved"],
            state["request"],
            threshold=threshold,
        )
        completed = _now()
        entry = StageTraceEntry(
            stage="critic",
            attempt=current_attempt,
            status="ok",
            started_at=started,
            completed_at=completed,
            duration_ms=_ms_between(started, completed),
            model_calls=calls,
            verdict=verdict,
            error_message=None,
        )
        await _emit_event(state, entry)

        # Snapshot this attempt's outputs for end-of-run winner selection.
        attempts: list[AttemptResult] = list(state.get("attempts") or [])
        attempts.append(
            AttemptResult(
                attempt=current_attempt,
                ad_copy=state["ad_copy"],
                image=state["image"],
                image_path=Path(state["image_path"]),
                critic=verdict,
            )
        )

        retry_cap = int(state["retry_cap"])
        will_retry = (not verdict.passed) and current_attempt < retry_cap + 1
        next_attempt = current_attempt + 1 if will_retry else current_attempt

        return {
            "critic": verdict,
            "attempts": attempts,
            "attempt": next_attempt,
            "will_retry": will_retry,
            "prior_critic_feedback": verdict.feedback if will_retry else None,
        }
    except StageError as exc:
        completed = _now()
        entry = StageTraceEntry(
            stage="critic",
            attempt=current_attempt,
            status="failed",
            started_at=started,
            completed_at=completed,
            duration_ms=_ms_between(started, completed),
            model_calls=[],
            verdict=None,
            error_message=str(exc),
        )
        await _emit_event(state, entry)
        raise _stage_error("critic", str(exc)) from exc


def _critic_router(state: GraphState) -> str:
    """Route from critic to either copy (retry) or END."""

    return "copy" if state.get("will_retry") else END


# ---------------------------------------------------------------------------
# Graph construction.
# ---------------------------------------------------------------------------


def build_graph() -> Any:
    """Return a compiled LangGraph for the Aura pipeline."""

    sg: StateGraph[GraphState] = StateGraph(GraphState)
    sg.add_node("research", _research_node)
    sg.add_node("retrieval", _retrieval_node)
    sg.add_node("copy", _copy_node)
    sg.add_node("image", _image_node)
    sg.add_node("critic", _critic_node)

    sg.add_edge(START, "research")
    sg.add_edge(START, "retrieval")
    sg.add_edge("research", "copy")
    sg.add_edge("retrieval", "copy")
    sg.add_edge("copy", "image")
    sg.add_edge("image", "critic")
    sg.add_conditional_edges("critic", _critic_router, {"copy": "copy", END: END})

    return sg.compile()


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


async def run(
    request: CampaignRequest,
    *,
    run_id: str,
    retry_cap: int,
    critic_threshold: float,
    artifacts_dir: Path,
    chroma_client: Any,
    on_stage_event: OnStageEvent | None = None,
) -> tuple[list[AttemptResult] | None, StageName | None, str | None]:
    """Execute the pipeline, returning every attempt the critic produced.

    Returns ``(attempts, failed_stage, failed_reason)``. When ``failed_stage``
    is ``None`` the run succeeded and ``attempts`` is a non-empty list ordered
    by attempt number (the runner picks the highest-scoring attempt as the
    winner). When ``failed_stage`` is set, ``attempts`` is ``None``.
    """

    state: GraphState = {
        "request": request,
        "run_id": run_id,
        "retry_cap": retry_cap,
        "critic_threshold": critic_threshold,
        "attempt": 1,
        "artifacts_dir": str(artifacts_dir),
        "chroma_client": chroma_client,
        "on_stage_event": on_stage_event or (lambda _entry: None),
        "attempts": [],
        "prior_critic_feedback": None,
        "will_retry": False,
    }

    graph = build_graph()

    try:
        final = await graph.ainvoke(state)
    except StageError as exc:
        stage = getattr(exc, "stage", None)
        return None, stage, str(exc)

    attempts = list(final.get("attempts") or [])
    if not attempts:
        return None, "critic", "no critic verdict was produced"
    return attempts, None, None


__all__ = ["AttemptResult", "GraphState", "build_graph", "run"]
