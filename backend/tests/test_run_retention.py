"""FR-022 / FR-023 — per-brand run retention pruning.

When a brand accumulates more than ``cap`` terminal runs, the oldest must
be deleted along with their ``stage_traces`` rows (FK cascade), their
``campaign_outputs`` row (FK cascade), and the on-disk artifact PNG. The
``cap`` most-recent runs survive.

The test exercises the repository method directly with the same
``on_pruned`` callback wiring the runner uses (see
``backend/orchestrator/runner.py::_after_terminal_hooks``).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from backend.persistence import session as session_module
from backend.persistence.repository import (
    BrandRepository,
    RunRepository,
    StageTraceRepository,
)
from sqlalchemy.engine import Engine


def _seed_terminal_run(
    session: session_module.Session,
    *,
    run_id: str,
    brand_id: str,
    submitted_at: datetime,
    artifacts_dir: Path,
) -> Path:
    """Create a fully-formed terminal run + trace + output + on-disk PNG."""

    run = RunRepository.create_queued(
        session,
        run_id=run_id,
        brand_id=brand_id,
        brief="brief",
        platform="instagram",
        target_audience="aud",
        retry_cap=2,
        critic_threshold=0.7,
    )
    run.submitted_at = submitted_at
    run.status = "done"
    run.started_at = submitted_at + timedelta(seconds=1)
    run.completed_at = submitted_at + timedelta(seconds=10)

    StageTraceRepository.write_completed_trace(
        session,
        run_id=run_id,
        attempt=1,
        stage="copy",
        status="ok",
        started_at=submitted_at + timedelta(seconds=2),
        completed_at=submitted_at + timedelta(seconds=3),
        duration_ms=1000,
        inputs_json="{}",
        outputs_json="{}",
        model_calls_json="[]",
    )

    image_path = artifacts_dir / brand_id / f"{run_id}.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + run_id.encode())

    RunRepository.write_campaign_output(
        session,
        run_id=run_id,
        winning_attempt=1,
        headline="h",
        primary_text="p",
        cta="c",
        image_path=f"/api/v1/artifacts/{brand_id}/{run_id}.png",
        image_width=64,
        image_height=64,
        final_score_overall=0.85,
        final_score_breakdown_json="{}",
        final_score_passed=True,
        final_score_feedback="ok",
    )

    return image_path


def test_oldest_pruned_when_over_cap(engine: Engine, tmp_data_dir: Path) -> None:
    """cap+1 runs → oldest deleted (run + trace + output + PNG); newest cap survive."""

    cap = 3
    brand_id = "01HXRETENTIONBRAND"
    artifacts_dir = tmp_data_dir / "artifacts"

    # Newest first so submitted_at goes 100→0 minutes ago — the prune
    # keeps the *most recent* cap runs (i.e. the first `cap` ids inserted
    # here once ordered DESC).
    base = datetime.now(tz=UTC)
    seeded: list[tuple[str, Path]] = []
    with session_module.SessionLocal() as s:
        BrandRepository.create(s, brand_id=brand_id, display_name="Retention Co")
        for i in range(cap + 1):
            run_id = f"01HXRUN{i:020d}"
            path = _seed_terminal_run(
                s,
                run_id=run_id,
                brand_id=brand_id,
                submitted_at=base - timedelta(minutes=i * 10),
                artifacts_dir=artifacts_dir,
            )
            seeded.append((run_id, path))
        s.commit()

    # The oldest run is the *last* one inserted (largest minutes-ago offset).
    newest_run_ids = [rid for rid, _ in seeded[:cap]]
    oldest_run_id, oldest_image_path = seeded[cap]
    assert oldest_image_path.exists(), "fixture pre-condition: oldest PNG was written"

    pruned_ids: list[str] = []

    def _on_pruned(ids: Iterable[str]) -> None:
        for rid in ids:
            pruned_ids.append(str(rid))
            (artifacts_dir / brand_id / f"{rid}.png").unlink(missing_ok=True)

    with session_module.SessionLocal() as s:
        result = RunRepository.prune_oldest_terminal_for_brand(
            s,
            brand_id,
            cap=cap,
            on_pruned=_on_pruned,
        )
        s.commit()

    assert result == [oldest_run_id], result
    assert pruned_ids == [oldest_run_id]

    # The PNG for the pruned run is gone; the surviving runs' PNGs remain.
    assert not oldest_image_path.exists()
    for rid, path in seeded[:cap]:
        assert path.exists(), rid

    # Database state: oldest run + its trace + output gone via FK cascade;
    # the cap newest runs remain along with their children.
    with session_module.SessionLocal() as s:
        assert RunRepository.get(s, oldest_run_id) is None
        assert StageTraceRepository.list_for_run(s, oldest_run_id) == []
        for rid in newest_run_ids:
            row = RunRepository.get_with_trace_and_output(s, rid)
            assert row is not None, rid
            assert row.output is not None, rid
            assert len(row.stage_traces) == 1, rid


def test_prune_noop_when_under_cap(engine: Engine, tmp_data_dir: Path) -> None:
    """At-cap (or under) means zero rows are pruned."""

    cap = 5
    brand_id = "01HXRETENTIONBRAND2"
    artifacts_dir = tmp_data_dir / "artifacts"

    base = datetime.now(tz=UTC)
    with session_module.SessionLocal() as s:
        BrandRepository.create(s, brand_id=brand_id, display_name="Under-Cap Co")
        for i in range(cap):
            _seed_terminal_run(
                s,
                run_id=f"01HXUNDER{i:019d}",
                brand_id=brand_id,
                submitted_at=base - timedelta(minutes=i),
                artifacts_dir=artifacts_dir,
            )
        s.commit()

    with session_module.SessionLocal() as s:
        result = RunRepository.prune_oldest_terminal_for_brand(s, brand_id, cap=cap)
        s.commit()

    assert result == []
