"""Verify the MLflow shape :func:`log_aura_run_to_mlflow` produces.

This test stubs out the underlying :func:`eval.tracking.log_run` and
asserts the params, metrics, and artifacts dict that ``stage_tracking``
hands it. The exact keys are part of Aura's developer-facing observability
contract (per `quickstart.md §11`); regressions here would silently
break the team's ability to compare runs across MLflow.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from agents.schemas import (
    AdCopy,
    Campaign,
    CampaignRequest,
    CriticScore,
    GeneratedImage,
    ModelCall,
    RunRecord,
    StageTraceEntry,
)


def _make_run_record(*, brand_id: str = "01HXBR", run_id: str = "01HXRUN") -> RunRecord:
    base = datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC)
    request = CampaignRequest(
        brief="Launch summer line.",
        platform="instagram",
        brand_id=brand_id,
        target_audience="18-24 urban",
    )
    trace = [
        StageTraceEntry(
            stage="research",
            attempt=1,
            status="degraded",
            started_at=base,
            completed_at=base + timedelta(seconds=4),
            duration_ms=4000,
            model_calls=[],
            verdict=None,
            error_message="tavily disabled",
        ),
        StageTraceEntry(
            stage="retrieval",
            attempt=1,
            status="ok",
            started_at=base + timedelta(seconds=4),
            completed_at=base + timedelta(seconds=5),
            duration_ms=1000,
            model_calls=[],
        ),
        StageTraceEntry(
            stage="copy",
            attempt=1,
            status="ok",
            started_at=base + timedelta(seconds=5),
            completed_at=base + timedelta(seconds=7),
            duration_ms=2000,
            model_calls=[
                ModelCall(
                    provider="openrouter",
                    model="openai/gpt-4o-mini",
                    op="chat.completions",
                    latency_ms=1800,
                    token_in=400,
                    token_out=120,
                )
            ],
        ),
        StageTraceEntry(
            stage="image",
            attempt=1,
            status="ok",
            started_at=base + timedelta(seconds=7),
            completed_at=base + timedelta(seconds=10),
            duration_ms=3000,
            model_calls=[
                ModelCall(
                    provider="openrouter",
                    model="google/gemini-2.5-flash-image-preview",
                    op="chat.completions",
                    latency_ms=2900,
                )
            ],
        ),
        StageTraceEntry(
            stage="critic",
            attempt=1,
            status="ok",
            started_at=base + timedelta(seconds=10),
            completed_at=base + timedelta(seconds=12),
            duration_ms=2000,
            model_calls=[
                ModelCall(
                    provider="openrouter",
                    model="openai/gpt-4o-mini",
                    op="chat.completions",
                    latency_ms=1900,
                )
            ],
            verdict=CriticScore(
                overall=0.85,
                breakdown={
                    "relevance": 0.9,
                    "brand_fit": 0.85,
                    "clarity": 0.82,
                    "factual_alignment": 0.83,
                },
                feedback="Solid.",
                passed=True,
            ),
        ),
    ]
    output = Campaign(
        request=request,
        ad_copy=AdCopy(
            headline="Headline",
            primary_text="Body",
            cta="Shop",
            platform="instagram",
        ),
        image=GeneratedImage(
            path=f"/api/v1/artifacts/{brand_id}/{run_id}.png",
            prompt="prompt",
            negative_prompt="",
            dimensions=(1024, 1024),
        ),
        score=CriticScore(
            overall=0.85,
            breakdown={
                "relevance": 0.9,
                "brand_fit": 0.85,
                "clarity": 0.82,
                "factual_alignment": 0.83,
            },
            feedback="Solid.",
            passed=True,
        ),
        run_id=run_id,
    )
    return RunRecord(
        id=run_id,
        brand_id=brand_id,
        request=request,
        status="done",
        current_stage=None,
        attempt_count=1,
        retry_cap=2,
        critic_threshold=0.7,
        failed_stage=None,
        failed_reason=None,
        submitted_at=base,
        started_at=base,
        completed_at=base + timedelta(seconds=12),
        trace=trace,
        output=output,
    )


def test_log_aura_run_to_mlflow_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The MLflow run carries the documented params, metrics, and artifacts."""

    captured: dict[str, Any] = {}

    def _fake_log_run(
        params: Mapping[str, Any],
        metrics: Mapping[str, float],
        artifacts: Iterable[str | Path] = (),
        *,
        run_name: str | None = None,
        experiment: str | None = None,
        tags: Mapping[str, str] | None = None,
    ) -> str:
        captured["params"] = dict(params)
        captured["metrics"] = dict(metrics)
        captured["artifacts"] = [Path(a).name for a in artifacts]
        captured["run_name"] = run_name
        captured["tags"] = dict(tags) if tags else {}
        return "mlflow-run-id-stub"

    monkeypatch.setattr("eval.stage_tracking.log_run", _fake_log_run)

    image_path = tmp_path / "image.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n_test_image_")

    record = _make_run_record()
    from eval.stage_tracking import log_aura_run_to_mlflow

    result = log_aura_run_to_mlflow(record, image_path)

    assert result == "mlflow-run-id-stub"

    # Required params (per quickstart §11).
    for key in (
        "brand_id",
        "platform",
        "brief_chars",
        "attempt_count",
        "retry_cap",
        "critic_threshold",
        "status",
    ):
        assert key in captured["params"], (key, captured["params"])
    assert captured["params"]["brand_id"] == record.brand_id
    assert captured["params"]["platform"] == "instagram"
    assert captured["params"]["status"] == "done"

    # Per-stage duration metrics + total + per-dimension critic metrics.
    metrics = captured["metrics"]
    for stage in ("research", "retrieval", "copy", "image", "critic"):
        assert f"duration_{stage}_ms" in metrics, stage
        assert metrics[f"duration_{stage}_ms"] >= 0
    assert (
        metrics["total_duration_ms"]
        >= sum(
            metrics[f"duration_{s}_ms"]
            for s in ("research", "retrieval", "copy", "image", "critic")
        )
        - 1e-6
    )
    assert "critic_overall" in metrics
    for dim in ("relevance", "brand_fit", "clarity", "factual_alignment"):
        assert f"critic_{dim}" in metrics, dim

    # The three documented artifacts: trace.json, ad_copy.json, final_image.png.
    assert set(captured["artifacts"]) == {"trace.json", "ad_copy.json", "final_image.png"}

    # Tags carry the cross-reference back to Aura ids.
    assert captured["tags"].get("aura_run_id") == record.id
    assert captured["tags"].get("aura_brand_id") == record.brand_id


def test_log_aura_run_swallows_mlflow_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """MLflow downtime must NEVER break a run — log_aura_run_to_mlflow returns None."""

    def _boom(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("mlflow unreachable")

    monkeypatch.setattr("eval.stage_tracking.log_run", _boom)

    from eval.stage_tracking import log_aura_run_to_mlflow

    record = _make_run_record()
    assert log_aura_run_to_mlflow(record, image_path=None) is None
