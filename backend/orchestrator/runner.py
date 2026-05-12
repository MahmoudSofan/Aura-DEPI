"""CampaignRunner — async semaphore-bounded queue consumer for campaign runs.

Lifecycle
---------

1. ``CampaignRunner.__init__`` — captures config (concurrency cap, retry
   cap, threshold, artifacts dir, chroma client factory).
2. ``await runner.start()`` — launches the queue-consumer background task
   inside the FastAPI lifespan.
3. ``runner.enqueue(run_id)`` — non-blocking; the API handler calls this
   right after persisting the ``runs`` row in ``status='queued'``.
4. The consumer pops one queued run_id at a time, awaits a semaphore slot,
   then runs ``_run_one`` which invokes :func:`agents.graph.run`, persists
   the stage traces as they arrive, writes the campaign output (or sets
   ``failed_stage``/``failed_reason``), writes the MLflow run, and prunes
   the oldest terminal runs for the brand (FR-022).
5. ``await runner.stop()`` — cancels the consumer task on shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agents.graph import AttemptResult
from agents.graph import run as graph_run
from agents.schemas import (
    CampaignRequest,
    ModelCall,
    RunRecord,
    StageName,
    StageTraceEntry,
)
from backend.persistence.repository import RunRepository, StageTraceRepository
from backend.persistence.session import session_scope
from eval.stage_tracking import log_aura_run_to_mlflow

logger = logging.getLogger("aura.orchestrator.runner")


def _resolve_artifacts_dir() -> Path:
    return Path(os.getenv("AURA_DATA_DIR", "backend/data")) / "artifacts"


def _resolve_concurrency_cap() -> int:
    raw = os.getenv("AURA_CONCURRENCY_CAP", "5")
    try:
        return max(1, int(raw))
    except ValueError:
        return 5


def _resolve_retention_cap() -> int:
    raw = os.getenv("AURA_RUN_RETENTION_PER_BRAND", "100")
    try:
        return max(1, int(raw))
    except ValueError:
        return 100


def _make_default_chroma_client() -> Any:
    """Build the default Chroma client based on env.

    Tests monkey-patch this module-level function (or pass a ``chroma_client_factory``
    to the runner constructor) to inject an in-process ephemeral client.
    """

    host = os.getenv("CHROMA_HOST")
    port_raw = os.getenv("CHROMA_PORT")
    import chromadb

    if host and port_raw:
        try:
            return chromadb.HttpClient(host=host, port=int(port_raw))
        except Exception as exc:  # noqa: BLE001
            logger.warning("HttpClient init failed (%s); falling back to EphemeralClient", exc)
    return chromadb.EphemeralClient()


class CampaignRunner:
    """Async runner that owns the run queue and concurrency cap."""

    def __init__(
        self,
        *,
        concurrency_cap: int | None = None,
        artifacts_dir: Path | None = None,
        retention_cap: int | None = None,
        chroma_client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._concurrency_cap = concurrency_cap or _resolve_concurrency_cap()
        self._artifacts_dir = artifacts_dir or _resolve_artifacts_dir()
        self._retention_cap = retention_cap or _resolve_retention_cap()
        self._chroma_factory = chroma_client_factory or _make_default_chroma_client

        self._semaphore = asyncio.Semaphore(self._concurrency_cap)
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._consumer_task: asyncio.Task[None] | None = None
        self._running_runs: set[str] = set()
        self._stopped = asyncio.Event()
        self._completed_events: dict[str, asyncio.Event] = {}
        self._completed_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle.
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._consumer_task is not None:
            return
        self._stopped.clear()
        self._consumer_task = asyncio.create_task(self._consume(), name="aura-campaign-consumer")

    async def stop(self) -> None:
        self._stopped.set()
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._consumer_task
            self._consumer_task = None

    # ------------------------------------------------------------------
    # Submission.
    # ------------------------------------------------------------------

    def enqueue(self, run_id: str) -> None:
        """Non-blocking enqueue (called from API handlers)."""

        self._queue.put_nowait(run_id)

    async def wait_for(self, run_id: str, *, timeout: float = 60.0) -> None:
        """Block until the given run finishes (test-only convenience)."""

        async with self._completed_lock:
            event = self._completed_events.setdefault(run_id, asyncio.Event())
        await asyncio.wait_for(event.wait(), timeout=timeout)

    # ------------------------------------------------------------------
    # Internals.
    # ------------------------------------------------------------------

    async def _consume(self) -> None:
        while not self._stopped.is_set():
            try:
                run_id = await self._queue.get()
            except asyncio.CancelledError:
                return
            asyncio.create_task(self._dispatch(run_id), name=f"aura-run-{run_id}")

    async def _dispatch(self, run_id: str) -> None:
        async with self._semaphore:
            self._running_runs.add(run_id)
            try:
                await self._run_one(run_id)
            finally:
                self._running_runs.discard(run_id)
                async with self._completed_lock:
                    event = self._completed_events.setdefault(run_id, asyncio.Event())
                    event.set()

    async def _run_one(self, run_id: str) -> None:
        request, retry_cap, threshold, brand_id = await asyncio.to_thread(
            self._fetch_run_request, run_id
        )
        if request is None or brand_id is None:
            logger.warning("runner: run %s no longer exists; skipping", run_id)
            return

        await asyncio.to_thread(self._mark_running, run_id, "research")

        chroma_client = self._chroma_factory()

        async def on_stage_event(entry: StageTraceEntry) -> None:
            await asyncio.to_thread(self._persist_trace, run_id, entry)
            await asyncio.to_thread(self._mark_current_stage, run_id, entry.stage)

        try:
            attempts, failed_stage, failed_reason = await graph_run(
                request,
                run_id=run_id,
                retry_cap=retry_cap,
                critic_threshold=threshold,
                artifacts_dir=self._artifacts_dir,
                chroma_client=chroma_client,
                on_stage_event=on_stage_event,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("runner: pipeline crashed for run %s", run_id)
            await asyncio.to_thread(
                self._finalise_failed,
                run_id,
                failed_stage=None,
                failed_reason=f"pipeline_exception: {exc}",
            )
            return

        if not attempts:
            await asyncio.to_thread(
                self._finalise_failed,
                run_id,
                failed_stage=failed_stage,
                failed_reason=failed_reason or "unspecified",
            )
            return

        winner = max(attempts, key=lambda a: a.critic.overall)
        canonical_path = self._artifacts_dir / brand_id / f"{run_id}.png"
        await asyncio.to_thread(
            self._promote_winner_image,
            winner.image_path,
            canonical_path,
            [a.image_path for a in attempts],
        )

        await asyncio.to_thread(
            self._finalise_done,
            run_id,
            request,
            winner,
            canonical_path,
            len(attempts),
        )

        await asyncio.to_thread(self._after_terminal_hooks, run_id, brand_id, canonical_path)

    # ------------------------------------------------------------------
    # Sync helpers (executed in `asyncio.to_thread`).
    # ------------------------------------------------------------------

    def _fetch_run_request(
        self, run_id: str
    ) -> tuple[CampaignRequest | None, int, float, str | None]:
        with session_scope() as session:
            run = RunRepository.get(session, run_id)
            if run is None:
                return None, 0, 0.0, None
            request = CampaignRequest(
                brief=run.brief,
                platform=run.platform,  # type: ignore[arg-type]
                brand_id=run.brand_id,
                target_audience=run.target_audience,
            )
            return request, int(run.retry_cap), float(run.critic_threshold), run.brand_id

    def _mark_running(self, run_id: str, current_stage: StageName) -> None:
        with session_scope() as session:
            RunRepository.transition_to_running(session, run_id, current_stage=current_stage)

    def _mark_current_stage(self, run_id: str, stage: StageName) -> None:
        with session_scope() as session:
            RunRepository.update_stage(session, run_id, stage)

    def _persist_trace(self, run_id: str, entry: StageTraceEntry) -> None:
        verdict_json = entry.verdict.model_dump_json() if entry.verdict is not None else None
        model_calls_json = json.dumps([c.model_dump(mode="json") for c in entry.model_calls])
        with session_scope() as session:
            StageTraceRepository.write_completed_trace(
                session,
                run_id=run_id,
                attempt=entry.attempt,
                stage=entry.stage,
                status=entry.status,
                started_at=entry.started_at,
                completed_at=entry.completed_at,
                duration_ms=entry.duration_ms,
                inputs_json="{}",  # T031 doesn't yet pass per-stage inputs verbatim
                outputs_json="{}",
                model_calls_json=model_calls_json,
                verdict_json=verdict_json,
                error_message=entry.error_message,
            )

    def _finalise_failed(
        self,
        run_id: str,
        *,
        failed_stage: StageName | None,
        failed_reason: str,
    ) -> None:
        with session_scope() as session:
            RunRepository.transition_to_failed(
                session,
                run_id,
                failed_stage=failed_stage,
                failed_reason=failed_reason,
            )

    def _finalise_done(
        self,
        run_id: str,
        request: CampaignRequest,
        winner: AttemptResult,
        artifact_path: Path,
        attempt_count: int,
    ) -> None:
        del request  # parameter kept for symmetry with future audit hooks
        score = winner.critic
        ad_copy = winner.ad_copy
        image = winner.image
        with session_scope() as session:
            RunRepository.write_campaign_output(
                session,
                run_id=run_id,
                winning_attempt=winner.attempt,
                headline=ad_copy.headline,
                primary_text=ad_copy.primary_text,
                cta=ad_copy.cta,
                image_path=image.path,
                image_width=image.dimensions[0],
                image_height=image.dimensions[1],
                final_score_overall=score.overall,
                final_score_breakdown_json=json.dumps(score.breakdown),
                final_score_passed=bool(score.passed),
                final_score_feedback=score.feedback,
            )
            run = RunRepository.get(session, run_id)
            if run is not None:
                run.attempt_count = attempt_count
                session.flush()
            RunRepository.transition_to_done(session, run_id)

    def _promote_winner_image(
        self,
        winner_path: Path,
        canonical_path: Path,
        all_attempt_paths: list[Path],
    ) -> None:
        """Copy the winning attempt's PNG to ``{run_id}.png`` and clean up the rest."""

        canonical_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if winner_path.exists():
                shutil.copyfile(winner_path, canonical_path)
        except OSError as exc:
            logger.warning("failed to promote winning image %s: %s", winner_path, exc)
            return

        for path in all_attempt_paths:
            try:
                if path.resolve() == canonical_path.resolve():
                    continue
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("failed to delete attempt image %s: %s", path, exc)

    def _after_terminal_hooks(
        self,
        run_id: str,
        brand_id: str,
        artifact_path: Path,
    ) -> None:
        # Build a RunRecord snapshot for MLflow (and any future logger).
        record = self._build_run_record(run_id)
        if record is not None:
            log_aura_run_to_mlflow(record, artifact_path if artifact_path.exists() else None)

        # FR-022 retention pruning. Best-effort — log on failure.
        try:
            with session_scope() as session:
                pruned = RunRepository.prune_oldest_terminal_for_brand(
                    session,
                    brand_id,
                    cap=self._retention_cap,
                    on_pruned=lambda ids: self._delete_artifacts_for_runs(brand_id, ids),
                )
                if pruned:
                    logger.info("retention: pruned %d run(s) for brand %s", len(pruned), brand_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("retention prune failed for brand %s: %s", brand_id, exc)

    def _delete_artifacts_for_runs(self, brand_id: str, run_ids: Any) -> None:
        for rid in run_ids:
            path = self._artifacts_dir / brand_id / f"{rid}.png"
            try:
                path.unlink(missing_ok=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("failed to delete pruned artifact %s: %s", path, exc)

    def _build_run_record(self, run_id: str) -> RunRecord | None:
        with session_scope() as session:
            run = RunRepository.get_with_trace_and_output(session, run_id)
            if run is None:
                return None

            request = CampaignRequest(
                brief=run.brief,
                platform=run.platform,  # type: ignore[arg-type]
                brand_id=run.brand_id,
                target_audience=run.target_audience,
            )

            trace_entries: list[StageTraceEntry] = []
            for tr in sorted(run.stage_traces, key=lambda r: r.id):
                model_calls = [
                    ModelCall(**call) for call in json.loads(tr.model_calls_json or "[]")
                ]
                verdict = None
                if tr.verdict_json:
                    from agents.schemas import CriticScore

                    verdict = CriticScore.model_validate_json(tr.verdict_json)
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

            campaign = None
            if run.output is not None:
                from agents.schemas import AdCopy, Campaign, CriticScore, GeneratedImage

                breakdown = json.loads(run.output.final_score_breakdown_json or "{}")
                campaign = Campaign(
                    request=request,
                    ad_copy=AdCopy(
                        headline=run.output.headline,
                        primary_text=run.output.primary_text,
                        cta=run.output.cta,
                        platform=run.platform,  # type: ignore[arg-type]
                    ),
                    image=GeneratedImage(
                        path=run.output.image_path,
                        prompt="",
                        negative_prompt="",
                        dimensions=(run.output.image_width, run.output.image_height),
                    ),
                    score=CriticScore(
                        overall=run.output.final_score_overall,
                        breakdown=breakdown,
                        feedback=run.output.final_score_feedback,
                        passed=bool(run.output.final_score_passed),
                    ),
                    run_id=run_id,
                )

            return RunRecord(
                id=run.id,
                brand_id=run.brand_id,
                request=request,
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
                trace=trace_entries,
                output=campaign,
            )


__all__ = ["CampaignRunner"]
