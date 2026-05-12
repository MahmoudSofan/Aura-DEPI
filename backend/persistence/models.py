"""SQLAlchemy 2.x ORM models for Aura.

Schema follows
``specs/001-aura-marketing-platform/data-model.md``. All foreign keys cascade
on delete (FR-024). Timestamps are stored as ISO-8601 UTC strings (TEXT
affinity) populated in the application layer.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base for Aura ORM models."""


_PLATFORMS = ("facebook", "instagram", "tiktok", "twitter", "linkedin", "youtube")
_DOC_FORMATS = ("pdf", "docx", "txt", "md")
_RUN_STATUSES = ("queued", "running", "done", "failed")
_STAGE_NAMES = ("research", "retrieval", "copy", "image", "critic")
_STAGE_STATUSES = ("ok", "degraded", "failed")
_PARSE_STATUSES = ("parsed", "rejected")


def _enum_check(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


class Brand(Base):
    __tablename__ = "brands"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    documents: Mapped[list[Document]] = relationship(
        back_populates="brand",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    runs: Mapped[list[Run]] = relationship(
        back_populates="brand",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint(
            "length(display_name) BETWEEN 1 AND 200",
            name="ck_brands_display_name_length",
        ),
    )


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    brand_id: Mapped[str] = mapped_column(
        String, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False
    )
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    format: Mapped[str] = mapped_column(String(8), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parse_status: Mapped[str] = mapped_column(String(16), nullable=False)
    parse_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    brand: Mapped[Brand] = relationship(back_populates="documents")

    __table_args__ = (
        UniqueConstraint("brand_id", "content_hash", name="uq_documents_brand_content_hash"),
        Index("ix_documents_brand_created", "brand_id", "created_at"),
        CheckConstraint(_enum_check("format", _DOC_FORMATS), name="ck_documents_format"),
        CheckConstraint(
            _enum_check("parse_status", _PARSE_STATUSES),
            name="ck_documents_parse_status",
        ),
        CheckConstraint("byte_size > 0 AND byte_size <= 52428800", name="ck_documents_size"),
        CheckConstraint("chunk_count >= 0", name="ck_documents_chunk_count"),
        CheckConstraint(
            "length(original_filename) BETWEEN 1 AND 255",
            name="ck_documents_filename_length",
        ),
    )


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    brand_id: Mapped[str] = mapped_column(
        String, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False
    )
    brief: Mapped[str] = mapped_column(Text, nullable=False)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    target_audience: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    current_stage: Mapped[str | None] = mapped_column(String(16), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_cap: Mapped[int] = mapped_column(Integer, nullable=False)
    critic_threshold: Mapped[float] = mapped_column(Float, nullable=False)
    failed_stage: Mapped[str | None] = mapped_column(String(16), nullable=True)
    failed_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    brand: Mapped[Brand] = relationship(back_populates="runs")
    stage_traces: Mapped[list[StageTrace]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="StageTrace.id",
    )
    output: Mapped[CampaignOutput | None] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )

    __table_args__ = (
        Index("ix_runs_brand_submitted", "brand_id", "submitted_at"),
        Index("ix_runs_status", "status"),
        CheckConstraint(_enum_check("platform", _PLATFORMS), name="ck_runs_platform"),
        CheckConstraint(_enum_check("status", _RUN_STATUSES), name="ck_runs_status"),
        CheckConstraint(
            "current_stage IS NULL OR " + _enum_check("current_stage", _STAGE_NAMES),
            name="ck_runs_current_stage",
        ),
        CheckConstraint(
            "failed_stage IS NULL OR " + _enum_check("failed_stage", _STAGE_NAMES),
            name="ck_runs_failed_stage",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_runs_attempt_count"),
        CheckConstraint("retry_cap >= 0", name="ck_runs_retry_cap"),
        CheckConstraint("critic_threshold BETWEEN 0 AND 1", name="ck_runs_critic_threshold"),
        CheckConstraint("length(brief) BETWEEN 1 AND 5000", name="ck_runs_brief_length"),
        CheckConstraint(
            "length(target_audience) BETWEEN 1 AND 500",
            name="ck_runs_audience_length",
        ),
    )


class StageTrace(Base):
    __tablename__ = "stage_traces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    inputs_json: Mapped[str] = mapped_column(Text, nullable=False)
    outputs_json: Mapped[str] = mapped_column(Text, nullable=False)
    model_calls_json: Mapped[str] = mapped_column(Text, nullable=False)
    verdict_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped[Run] = relationship(back_populates="stage_traces")

    __table_args__ = (
        UniqueConstraint("run_id", "attempt", "stage", name="uq_stage_traces_run_attempt_stage"),
        Index("ix_stage_traces_run_attempt_stage", "run_id", "attempt", "stage"),
        CheckConstraint(_enum_check("stage", _STAGE_NAMES), name="ck_stage_traces_stage"),
        CheckConstraint(_enum_check("status", _STAGE_STATUSES), name="ck_stage_traces_status"),
        CheckConstraint("attempt >= 1", name="ck_stage_traces_attempt"),
        CheckConstraint("duration_ms >= 0", name="ck_stage_traces_duration"),
        CheckConstraint(
            "(status <> 'degraded') OR (stage = 'research')",
            name="ck_stage_traces_degraded_only_research",
        ),
    )


class CampaignOutput(Base):
    __tablename__ = "campaign_outputs"

    run_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("runs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    winning_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    primary_text: Mapped[str] = mapped_column(Text, nullable=False)
    cta: Mapped[str] = mapped_column(Text, nullable=False)
    image_path: Mapped[str] = mapped_column(Text, nullable=False)
    image_width: Mapped[int] = mapped_column(Integer, nullable=False)
    image_height: Mapped[int] = mapped_column(Integer, nullable=False)
    final_score_overall: Mapped[float] = mapped_column(Float, nullable=False)
    final_score_breakdown_json: Mapped[str] = mapped_column(Text, nullable=False)
    final_score_passed: Mapped[int] = mapped_column(Integer, nullable=False)
    final_score_feedback: Mapped[str] = mapped_column(Text, nullable=False)

    run: Mapped[Run] = relationship(back_populates="output")

    __table_args__ = (
        CheckConstraint("winning_attempt >= 1", name="ck_campaign_outputs_winning_attempt"),
        CheckConstraint(
            "final_score_overall BETWEEN 0 AND 1",
            name="ck_campaign_outputs_overall",
        ),
        CheckConstraint("final_score_passed IN (0, 1)", name="ck_campaign_outputs_passed"),
        CheckConstraint("image_width >= 1", name="ck_campaign_outputs_width"),
        CheckConstraint("image_height >= 1", name="ck_campaign_outputs_height"),
    )


__all__ = [
    "Base",
    "Brand",
    "CampaignOutput",
    "Document",
    "Run",
    "StageTrace",
]
