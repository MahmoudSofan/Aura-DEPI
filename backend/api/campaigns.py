"""Campaigns API — submit, status poll, list."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Literal

from agents.schemas import (
    AdCopy,
    CampaignRequest,
    CriticScore,
    ModelCall,
    Platform,
    RunStatus,
    StageName,
    StageTraceEntry,
)
from backend.orchestrator.progress import progress_for
from backend.orchestrator.runner import CampaignRunner
from backend.persistence.repository import BrandRepository, RunRepository
from backend.persistence.session import get_session
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from ulid import ULID

logger = logging.getLogger("aura.api.campaigns")

router = APIRouter(tags=["campaigns"])


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _retry_cap() -> int:
    raw = os.getenv("AURA_RETRY_CAP", "2")
    try:
        return max(0, int(raw))
    except ValueError:
        return 2


def _critic_threshold() -> float:
    raw = os.getenv("AURA_CRITIC_THRESHOLD", "0.7")
    try:
        return min(1.0, max(0.0, float(raw)))
    except ValueError:
        return 0.7


def _runner(request: Request) -> CampaignRunner:
    runner = getattr(request.app.state, "campaign_runner", None)
    if runner is None:
        raise HTTPException(status_code=503, detail="campaign runner is not started")
    if not isinstance(runner, CampaignRunner):
        raise HTTPException(status_code=503, detail="campaign runner has unexpected type")
    return runner


# ---------------------------------------------------------------------------
# Response shapes.
# ---------------------------------------------------------------------------


class RunAcknowledgement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    status: Literal["queued"] = "queued"


class RunSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    brand_id: str
    platform: Platform
    status: RunStatus
    current_stage: StageName | None = None
    attempt_count: int = Field(ge=0)
    submitted_at: datetime
    completed_at: datetime | None = None
    final_score_overall: float | None = Field(default=None, ge=0.0, le=1.0)


class Run(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    brand_id: str
    request: CampaignRequest
    status: RunStatus
    current_stage: StageName | None = None
    attempt_count: int
    retry_cap: int
    critic_threshold: float
    failed_stage: StageName | None = None
    failed_reason: str | None = None
    submitted_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    progress: float = Field(ge=0.0, le=1.0)
    trace: list[StageTraceEntry]
    output: CampaignOutput | None = None


class CampaignOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    winning_attempt: int = Field(ge=1)
    ad_copy: AdCopy
    image_url: str
    image_width: int = Field(ge=1)
    image_height: int = Field(ge=1)
    score: CriticScore


Run.model_rebuild()


# ---------------------------------------------------------------------------
# Routes.
# ---------------------------------------------------------------------------


@router.post(
    "/campaigns",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RunAcknowledgement,
    summary="Submit a campaign brief",
    operation_id="submitCampaign",
)
def submit_campaign(
    body: CampaignRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> RunAcknowledgement:
    brand = BrandRepository.get(session, body.brand_id)
    if brand is None:
        raise HTTPException(status_code=404, detail=f"brand_id {body.brand_id!r} not found")

    run_id = str(ULID())
    RunRepository.create_queued(
        session,
        run_id=run_id,
        brand_id=body.brand_id,
        brief=body.brief,
        platform=body.platform,
        target_audience=body.target_audience,
        retry_cap=_retry_cap(),
        critic_threshold=_critic_threshold(),
    )
    session.flush()
    session.commit()

    _runner(request).enqueue(run_id)
    logger.info("campaign queued run_id=%s brand_id=%s", run_id, body.brand_id)
    return RunAcknowledgement(run_id=run_id)


@router.get(
    "/campaigns",
    response_model=list[RunSummary],
    summary="List runs (most recent first), optionally filtered by brand",
    operation_id="listCampaigns",
)
def list_campaigns(
    brand_id: str | None = Query(default=None),
    status_filter: RunStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
) -> list[RunSummary]:
    runs = RunRepository.list_runs(
        session,
        brand_id=brand_id,
        status=status_filter,
        limit=limit,
    )
    return [
        RunSummary(
            id=r.id,
            brand_id=r.brand_id,
            platform=r.platform,  # type: ignore[arg-type]
            status=r.status,  # type: ignore[arg-type]
            current_stage=r.current_stage,  # type: ignore[arg-type]
            attempt_count=r.attempt_count,
            submitted_at=r.submitted_at,
            completed_at=r.completed_at,
            final_score_overall=(r.output.final_score_overall if r.output else None),
        )
        for r in runs
    ]


@router.get(
    "/campaigns/{run_id}",
    response_model=Run,
    summary="Fetch a run's full state, including per-stage trace",
    operation_id="getCampaign",
)
def get_campaign(
    run_id: str,
    session: Session = Depends(get_session),
) -> Run:
    run = RunRepository.get_with_trace_and_output(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run_id {run_id!r} not found")

    request_payload = CampaignRequest(
        brief=run.brief,
        platform=run.platform,  # type: ignore[arg-type]
        brand_id=run.brand_id,
        target_audience=run.target_audience,
    )

    trace_entries: list[StageTraceEntry] = []
    for tr in sorted(run.stage_traces, key=lambda x: (x.started_at, x.id)):
        model_calls = [ModelCall(**call) for call in json.loads(tr.model_calls_json or "[]")]
        verdict = CriticScore.model_validate_json(tr.verdict_json) if tr.verdict_json else None
        trace_entries.append(
            StageTraceEntry(
                stage=tr.stage,  # type: ignore[arg-type]
                attempt=tr.attempt,
                status=tr.status,  # type: ignore[arg-type]
                started_at=tr.started_at,
                completed_at=tr.completed_at,
                duration_ms=tr.duration_ms,
                model_calls=model_calls,
                verdict=verdict,
                error_message=tr.error_message,
            )
        )

    output_payload: CampaignOutput | None = None
    if run.output is not None and run.status == "done":
        breakdown = json.loads(run.output.final_score_breakdown_json or "{}")
        output_payload = CampaignOutput(
            winning_attempt=run.output.winning_attempt,
            ad_copy=AdCopy(
                headline=run.output.headline,
                primary_text=run.output.primary_text,
                cta=run.output.cta,
                platform=run.platform,  # type: ignore[arg-type]
            ),
            image_url=run.output.image_path,
            image_width=run.output.image_width,
            image_height=run.output.image_height,
            score=CriticScore(
                overall=run.output.final_score_overall,
                breakdown=breakdown,
                feedback=run.output.final_score_feedback,
                passed=bool(run.output.final_score_passed),
            ),
        )

    progress = progress_for(
        stage=run.current_stage,  # type: ignore[arg-type]
        status=run.status,  # type: ignore[arg-type]
        attempt_count=run.attempt_count,
        retry_cap=run.retry_cap,
    )

    return Run(
        id=run.id,
        brand_id=run.brand_id,
        request=request_payload,
        status=run.status,  # type: ignore[arg-type]
        current_stage=run.current_stage,  # type: ignore[arg-type]
        attempt_count=run.attempt_count,
        retry_cap=run.retry_cap,
        critic_threshold=run.critic_threshold,
        failed_stage=run.failed_stage,  # type: ignore[arg-type]
        failed_reason=run.failed_reason,
        submitted_at=run.submitted_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        progress=progress,
        trace=trace_entries,
        output=output_payload,
    )


__all__ = ["router", "Run", "RunAcknowledgement", "RunSummary", "CampaignOutput"]
