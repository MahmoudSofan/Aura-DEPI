"""MLflow logging hook called on every terminal Aura run.

The runner invokes :func:`log_aura_run_to_mlflow` after a run reaches
``done`` or ``failed``. Failures inside this helper are logged and
swallowed — MLflow downtime must NEVER break an Aura run (per
`research.md §10` and the spec's separation of operator-facing trace
(SQLite) from developer-facing experiment tracking (MLflow)).
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path

from agents.schemas import RunRecord
from eval.tracking import log_run

logger = logging.getLogger("aura.eval.stage_tracking")


def _stage_durations_ms(run: RunRecord) -> dict[str, float]:
    """Sum durations per stage across all attempts. Keys: ``duration_<stage>_ms``."""

    totals: dict[str, float] = {
        f"duration_{stage}_ms": 0.0
        for stage in ("research", "retrieval", "copy", "image", "critic")
    }
    grand_total = 0.0
    for entry in run.trace:
        key = f"duration_{entry.stage}_ms"
        totals[key] = totals.get(key, 0.0) + float(entry.duration_ms)
        grand_total += float(entry.duration_ms)
    totals["total_duration_ms"] = grand_total
    return totals


def _critic_metrics(run: RunRecord) -> dict[str, float]:
    """Extract critic dimension scores from the winning attempt's verdict."""

    if run.output is None:
        return {}
    verdict = run.output.score
    metrics: dict[str, float] = {"critic_overall": float(verdict.overall)}
    for dim, score in verdict.breakdown.items():
        metrics[f"critic_{dim}"] = float(score)
    return metrics


def log_aura_run_to_mlflow(run: RunRecord, image_path: Path | None) -> str | None:
    """Log one MLflow run for this Aura run; return its mlflow_run_id (or None on failure).

    The function never raises — MLflow downtime must not break an Aura run.
    """

    try:
        params: dict[str, object] = {
            "brand_id": run.brand_id,
            "platform": run.request.platform,
            "brief_chars": len(run.request.brief),
            "attempt_count": run.attempt_count,
            "retry_cap": run.retry_cap,
            "critic_threshold": run.critic_threshold,
            "status": run.status,
        }

        metrics: dict[str, float] = {}
        metrics.update(_stage_durations_ms(run))
        metrics.update(_critic_metrics(run))

        artifacts: list[Path] = []
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            trace_path = tmp_path / "trace.json"
            trace_path.write_text(
                json.dumps(
                    [entry.model_dump(mode="json") for entry in run.trace],
                    indent=2,
                ),
                encoding="utf-8",
            )
            artifacts.append(trace_path)

            if run.output is not None:
                copy_path = tmp_path / "ad_copy.json"
                copy_path.write_text(
                    json.dumps(run.output.ad_copy.model_dump(mode="json"), indent=2),
                    encoding="utf-8",
                )
                artifacts.append(copy_path)

            if image_path is not None and image_path.exists():
                final_image = tmp_path / "final_image.png"
                final_image.write_bytes(image_path.read_bytes())
                artifacts.append(final_image)

            return log_run(
                params=params,
                metrics=metrics,
                artifacts=artifacts,
                run_name=f"aura-{run.id}",
                tags={"aura_run_id": run.id, "aura_brand_id": run.brand_id},
            )
    except Exception as exc:
        logger.warning("MLflow logging for run %s failed: %s", run.id, exc)
        return None


__all__ = ["log_aura_run_to_mlflow"]
