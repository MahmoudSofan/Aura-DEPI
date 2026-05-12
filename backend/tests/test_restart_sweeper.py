"""FR-025 restart sweeper — broader test moved out of test_foundational.py."""

from __future__ import annotations

from datetime import UTC, datetime

from backend.persistence import session as session_module
from backend.persistence.repository import (
    BrandRepository,
    RunRepository,
    StageTraceRepository,
)
from sqlalchemy.engine import Engine


def test_sweep_marks_queued_and_running_failed(engine: Engine) -> None:
    """Both queued and running rows transition to failed; trace preserved."""

    with session_module.SessionLocal() as s:
        BrandRepository.create(s, brand_id="01HXSWBRAND", display_name="Sw Co")

        RunRepository.create_queued(
            s,
            run_id="01HXQUEUED",
            brand_id="01HXSWBRAND",
            brief="brief A",
            platform="instagram",
            target_audience="aud",
            retry_cap=2,
            critic_threshold=0.7,
        )
        running = RunRepository.create_queued(
            s,
            run_id="01HXRUNNING",
            brand_id="01HXSWBRAND",
            brief="brief B",
            platform="linkedin",
            target_audience="aud",
            retry_cap=2,
            critic_threshold=0.7,
        )
        running.status = "running"
        running.started_at = datetime.now(tz=UTC)

        # Pre-existing trace row to confirm preservation across sweep.
        StageTraceRepository.write_completed_trace(
            s,
            run_id="01HXRUNNING",
            attempt=1,
            stage="research",
            status="ok",
            started_at=datetime.now(tz=UTC),
            completed_at=datetime.now(tz=UTC),
            duration_ms=10,
            inputs_json="{}",
            outputs_json="{}",
            model_calls_json="[]",
        )

        # Terminal row that must NOT be touched by the sweeper.
        terminal = RunRepository.create_queued(
            s,
            run_id="01HXDONE",
            brand_id="01HXSWBRAND",
            brief="brief C",
            platform="twitter",
            target_audience="aud",
            retry_cap=2,
            critic_threshold=0.7,
        )
        terminal.status = "done"
        terminal.completed_at = datetime.now(tz=UTC)
        s.commit()

    with session_module.SessionLocal() as s:
        swept = RunRepository.sweep_non_terminal_to_failed(s)
        s.commit()

    assert set(swept) == {"01HXQUEUED", "01HXRUNNING"}

    with session_module.SessionLocal() as s:
        for run_id in ("01HXQUEUED", "01HXRUNNING"):
            row = RunRepository.get(s, run_id)
            assert row is not None
            assert row.status == "failed"
            assert row.failed_reason == "interrupted_by_restart"
            assert row.completed_at is not None

        terminal_row = RunRepository.get(s, "01HXDONE")
        assert terminal_row is not None
        assert terminal_row.status == "done"
        assert terminal_row.failed_reason is None

        traces = StageTraceRepository.list_for_run(s, "01HXRUNNING")
        assert len(traces) == 1
        assert traces[0].stage == "research"
