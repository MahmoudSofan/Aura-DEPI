"""Repository classes that wrap SQLAlchemy queries for Aura.

All methods are sync and accept a :class:`Session`. Long-running calls inside
the orchestrator wrap them in :func:`asyncio.to_thread`. Repositories are
namespace classes (``classmethod`` only) so callers don't have to instantiate
them — keeps the call sites short while still grouping related queries.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime

from backend.persistence.models import Brand, CampaignOutput, Document, Run, StageTrace
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


class BrandRepository:
    """CRUD on :class:`Brand`."""

    @classmethod
    def create(cls, session: Session, *, brand_id: str, display_name: str) -> Brand:
        now = _utcnow()
        brand = Brand(
            id=brand_id,
            display_name=display_name,
            created_at=now,
            updated_at=now,
        )
        session.add(brand)
        session.flush()
        return brand

    @classmethod
    def get(cls, session: Session, brand_id: str) -> Brand | None:
        return session.get(Brand, brand_id)

    @classmethod
    def list_brands(cls, session: Session) -> list[Brand]:
        stmt = select(Brand).order_by(Brand.created_at.desc())
        return list(session.scalars(stmt).all())

    @classmethod
    def update_display_name(
        cls, session: Session, brand_id: str, display_name: str
    ) -> Brand | None:
        brand = cls.get(session, brand_id)
        if brand is None:
            return None
        brand.display_name = display_name
        brand.updated_at = _utcnow()
        session.flush()
        return brand

    @classmethod
    def delete(
        cls,
        session: Session,
        brand_id: str,
        *,
        on_after_commit: Callable[[str], None] | None = None,
    ) -> bool:
        """Delete a brand row; SQL FK cascade removes documents, runs, etc.

        ``on_after_commit`` runs once the SQL delete commits — used by the API
        layer to remove ChromaDB collections and on-disk uploads/artifacts in
        the same logical operation. The callback receives the brand_id.
        """

        brand = cls.get(session, brand_id)
        if brand is None:
            return False
        session.delete(brand)
        session.flush()
        if on_after_commit is not None:
            on_after_commit(brand_id)
        return True


class DocumentRepository:
    """CRUD on :class:`Document`."""

    @classmethod
    def create(
        cls,
        session: Session,
        *,
        document_id: str,
        brand_id: str,
        original_filename: str,
        format: str,
        byte_size: int,
        content_hash: str,
        storage_path: str,
        chunk_count: int,
        parse_status: str,
        parse_error: str | None = None,
    ) -> Document:
        document = Document(
            id=document_id,
            brand_id=brand_id,
            original_filename=original_filename,
            format=format,
            byte_size=byte_size,
            content_hash=content_hash,
            chunk_count=chunk_count,
            parse_status=parse_status,
            parse_error=parse_error,
            storage_path=storage_path,
            created_at=_utcnow(),
        )
        session.add(document)
        session.flush()
        return document

    @classmethod
    def list_for_brand(cls, session: Session, brand_id: str) -> list[Document]:
        stmt = (
            select(Document)
            .where(Document.brand_id == brand_id)
            .order_by(Document.created_at.desc())
        )
        return list(session.scalars(stmt).all())

    @classmethod
    def get_by_hash(cls, session: Session, brand_id: str, content_hash: str) -> Document | None:
        stmt = select(Document).where(
            Document.brand_id == brand_id,
            Document.content_hash == content_hash,
        )
        return session.scalars(stmt).one_or_none()


class RunRepository:
    """CRUD on :class:`Run` plus retention/sweep helpers."""

    @classmethod
    def create_queued(
        cls,
        session: Session,
        *,
        run_id: str,
        brand_id: str,
        brief: str,
        platform: str,
        target_audience: str,
        retry_cap: int,
        critic_threshold: float,
    ) -> Run:
        run = Run(
            id=run_id,
            brand_id=brand_id,
            brief=brief,
            platform=platform,
            target_audience=target_audience,
            status="queued",
            current_stage=None,
            attempt_count=0,
            retry_cap=retry_cap,
            critic_threshold=critic_threshold,
            failed_stage=None,
            failed_reason=None,
            submitted_at=_utcnow(),
            started_at=None,
            completed_at=None,
        )
        session.add(run)
        session.flush()
        return run

    @classmethod
    def get(cls, session: Session, run_id: str) -> Run | None:
        return session.get(Run, run_id)

    @classmethod
    def get_with_trace_and_output(cls, session: Session, run_id: str) -> Run | None:
        stmt = (
            select(Run)
            .where(Run.id == run_id)
            .options(selectinload(Run.stage_traces), selectinload(Run.output))
        )
        return session.scalars(stmt).one_or_none()

    @classmethod
    def transition_to_running(
        cls, session: Session, run_id: str, *, current_stage: str | None = None
    ) -> Run | None:
        run = cls.get(session, run_id)
        if run is None:
            return None
        run.status = "running"
        run.started_at = _utcnow()
        if current_stage is not None:
            run.current_stage = current_stage
        session.flush()
        return run

    @classmethod
    def update_stage(cls, session: Session, run_id: str, current_stage: str | None) -> None:
        run = cls.get(session, run_id)
        if run is None:
            return
        run.current_stage = current_stage
        session.flush()

    @classmethod
    def increment_attempt(cls, session: Session, run_id: str) -> int:
        run = cls.get(session, run_id)
        if run is None:
            return 0
        run.attempt_count += 1
        session.flush()
        return run.attempt_count

    @classmethod
    def transition_to_done(cls, session: Session, run_id: str) -> Run | None:
        run = cls.get(session, run_id)
        if run is None:
            return None
        run.status = "done"
        run.current_stage = None
        run.completed_at = _utcnow()
        session.flush()
        return run

    @classmethod
    def transition_to_failed(
        cls,
        session: Session,
        run_id: str,
        *,
        failed_stage: str | None,
        failed_reason: str,
    ) -> Run | None:
        run = cls.get(session, run_id)
        if run is None:
            return None
        run.status = "failed"
        run.failed_stage = failed_stage
        run.failed_reason = failed_reason
        run.completed_at = _utcnow()
        session.flush()
        return run

    @classmethod
    def transition_to_terminal(
        cls,
        session: Session,
        run_id: str,
        *,
        status: str,
        failed_stage: str | None = None,
        failed_reason: str | None = None,
    ) -> Run | None:
        if status == "done":
            return cls.transition_to_done(session, run_id)
        if status == "failed":
            return cls.transition_to_failed(
                session,
                run_id,
                failed_stage=failed_stage,
                failed_reason=failed_reason or "unspecified",
            )
        raise ValueError(f"transition_to_terminal: unsupported status {status!r}")

    @classmethod
    def list_runs(
        cls,
        session: Session,
        *,
        brand_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[Run]:
        stmt = select(Run).order_by(Run.submitted_at.desc())
        if brand_id is not None:
            stmt = stmt.where(Run.brand_id == brand_id)
        if status is not None:
            stmt = stmt.where(Run.status == status)
        stmt = stmt.limit(limit)
        return list(session.scalars(stmt).all())

    @classmethod
    def write_campaign_output(
        cls,
        session: Session,
        *,
        run_id: str,
        winning_attempt: int,
        headline: str,
        primary_text: str,
        cta: str,
        image_path: str,
        image_width: int,
        image_height: int,
        final_score_overall: float,
        final_score_breakdown_json: str,
        final_score_passed: bool,
        final_score_feedback: str,
    ) -> CampaignOutput:
        existing = session.get(CampaignOutput, run_id)
        if existing is not None:
            session.delete(existing)
            session.flush()
        output = CampaignOutput(
            run_id=run_id,
            winning_attempt=winning_attempt,
            headline=headline,
            primary_text=primary_text,
            cta=cta,
            image_path=image_path,
            image_width=image_width,
            image_height=image_height,
            final_score_overall=final_score_overall,
            final_score_breakdown_json=final_score_breakdown_json,
            final_score_passed=1 if final_score_passed else 0,
            final_score_feedback=final_score_feedback,
        )
        session.add(output)
        session.flush()
        return output

    @classmethod
    def prune_oldest_terminal_for_brand(
        cls,
        session: Session,
        brand_id: str,
        *,
        cap: int,
        on_pruned: Callable[[Iterable[str]], None] | None = None,
    ) -> list[str]:
        """Delete oldest terminal runs for the brand beyond ``cap`` (FR-022).

        Returns the list of pruned run_ids. ``on_pruned`` runs after the SQL
        delete flushes — used by the orchestrator to delete artifact PNGs.
        """

        terminal_stmt = (
            select(Run.id)
            .where(Run.brand_id == brand_id, Run.status.in_(("done", "failed")))
            .order_by(Run.submitted_at.desc())
        )
        terminal_ids = list(session.scalars(terminal_stmt).all())
        if len(terminal_ids) <= cap:
            return []

        to_prune = terminal_ids[cap:]
        if not to_prune:
            return []

        session.execute(delete(Run).where(Run.id.in_(to_prune)))
        session.flush()
        if on_pruned is not None:
            on_pruned(to_prune)
        return to_prune

    @classmethod
    def sweep_non_terminal_to_failed(
        cls, session: Session, *, reason: str = "interrupted_by_restart"
    ) -> list[str]:
        """Mark every non-terminal run as ``failed`` (FR-025).

        Called once on FastAPI startup. Returns the list of swept run_ids so
        callers can log how many were reaped.
        """

        stmt = select(Run).where(Run.status.in_(("queued", "running")))
        runs = list(session.scalars(stmt).all())
        now = _utcnow()
        ids: list[str] = []
        for run in runs:
            run.status = "failed"
            run.failed_reason = reason
            run.completed_at = now
            ids.append(run.id)
        session.flush()
        return ids


class StageTraceRepository:
    """Per-stage trace rows; one per ``(run_id, attempt, stage)``."""

    @classmethod
    def start_stage(
        cls,
        session: Session,
        *,
        run_id: str,
        attempt: int,
        stage: str,
        inputs_json: str,
    ) -> StageTrace:
        now = _utcnow()
        row = StageTrace(
            run_id=run_id,
            attempt=attempt,
            stage=stage,
            status="ok",
            started_at=now,
            completed_at=now,
            duration_ms=0,
            inputs_json=inputs_json,
            outputs_json="",
            model_calls_json="[]",
            verdict_json=None,
            error_message=None,
        )
        session.add(row)
        session.flush()
        return row

    @classmethod
    def complete_stage(
        cls,
        session: Session,
        *,
        run_id: str,
        attempt: int,
        stage: str,
        status: str,
        outputs_json: str,
        model_calls_json: str,
        verdict_json: str | None = None,
        error_message: str | None = None,
    ) -> StageTrace | None:
        stmt = select(StageTrace).where(
            StageTrace.run_id == run_id,
            StageTrace.attempt == attempt,
            StageTrace.stage == stage,
        )
        row = session.scalars(stmt).one_or_none()
        if row is None:
            return None
        now = _utcnow()
        row.status = status
        row.completed_at = now
        row.duration_ms = max(0, int((now - row.started_at).total_seconds() * 1000))
        row.outputs_json = outputs_json
        row.model_calls_json = model_calls_json
        row.verdict_json = verdict_json
        row.error_message = error_message
        session.flush()
        return row

    @classmethod
    def list_for_run(cls, session: Session, run_id: str) -> list[StageTrace]:
        stmt = (
            select(StageTrace)
            .where(StageTrace.run_id == run_id)
            .order_by(StageTrace.started_at, StageTrace.id)
        )
        return list(session.scalars(stmt).all())

    @classmethod
    def write_completed_trace(
        cls,
        session: Session,
        *,
        run_id: str,
        attempt: int,
        stage: str,
        status: str,
        started_at: datetime,
        completed_at: datetime,
        duration_ms: int,
        inputs_json: str,
        outputs_json: str,
        model_calls_json: str,
        verdict_json: str | None = None,
        error_message: str | None = None,
    ) -> StageTrace:
        """One-shot INSERT for a fully-formed stage trace row.

        Used by the runner's on_stage_event callback when the node already
        has start_at + completed_at + outputs in hand, avoiding the
        ``start_stage → complete_stage`` two-step round-trip.
        """

        row = StageTrace(
            run_id=run_id,
            attempt=attempt,
            stage=stage,
            status=status,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=duration_ms,
            inputs_json=inputs_json,
            outputs_json=outputs_json,
            model_calls_json=model_calls_json,
            verdict_json=verdict_json,
            error_message=error_message,
        )
        session.add(row)
        session.flush()
        return row


__all__ = [
    "BrandRepository",
    "DocumentRepository",
    "RunRepository",
    "StageTraceRepository",
]
