from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Platform = Literal["facebook", "instagram", "tiktok", "twitter", "linkedin", "youtube"]
RunStatus = Literal["queued", "running", "done", "failed"]
StageName = Literal["research", "retrieval", "copy", "image", "critic"]
StageStatus = Literal["ok", "degraded", "failed"]
DocumentFormat = Literal["pdf", "docx", "txt", "md"]


class CampaignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brief: str = Field(min_length=1, max_length=5000)
    platform: Platform
    brand_id: str = Field(min_length=1)
    target_audience: str = Field(min_length=1, max_length=500)


class ResearchOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trends: list[str]
    competitors: list[str]
    sources: list[str]


class Chunk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    source: str


class RetrievedContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunks: list[Chunk]
    brand_voice: str


class AdCopy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    headline: str = Field(min_length=1)
    primary_text: str = Field(min_length=1)
    cta: str = Field(min_length=1)
    platform: Platform


class GeneratedImage(BaseModel):
    """A generated marketing image artifact.

    `path` is the API-relative URL of the form
    ``/api/v1/artifacts/{brand_id}/{run_id}.png`` — never an absolute filesystem
    path and never inline base64. The bytes are served by
    `GET /api/v1/artifacts/{brand_id}/{filename}` (FR-023).
    """

    model_config = ConfigDict(extra="forbid")

    path: str
    prompt: str
    negative_prompt: str = ""
    dimensions: tuple[int, int]


class CriticScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overall: float = Field(ge=0.0, le=1.0)
    breakdown: dict[str, float]
    feedback: str
    passed: bool


class Campaign(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: CampaignRequest
    ad_copy: AdCopy
    image: GeneratedImage
    score: CriticScore
    run_id: str = Field(min_length=1)


class Brand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    display_name: str = Field(min_length=1, max_length=200)
    created_at: datetime
    updated_at: datetime


class DocumentRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    brand_id: str = Field(min_length=1)
    original_filename: str = Field(min_length=1, max_length=255)
    format: DocumentFormat
    byte_size: int = Field(gt=0, le=52_428_800)
    content_hash: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    chunk_count: int = Field(ge=0)
    parse_status: Literal["parsed", "rejected"]
    parse_error: str | None = None
    created_at: datetime


class ModelCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # `openrouter` — chat completions for copy / critic / image stages
    #                (image rides on chat with `modalities=["image","text"]`,
    #                since OpenRouter does not expose `/images/generations`).
    # `huggingface` — local sentence-transformers embedding (used inside the
    #                 retrieval stage; runs in-process, not a network call).
    # `tavily`     — web search (degradable research stage).
    provider: Literal["openrouter", "huggingface", "tavily"]
    model: str
    op: str
    latency_ms: int = Field(ge=0)
    token_in: int | None = None
    token_out: int | None = None


class StageTraceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: StageName
    attempt: int = Field(ge=1)
    status: StageStatus
    started_at: datetime
    completed_at: datetime
    duration_ms: int = Field(ge=0)
    model_calls: list[ModelCall]
    verdict: CriticScore | None = None
    error_message: str | None = None


class RunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    brand_id: str = Field(min_length=1)
    request: CampaignRequest
    status: RunStatus
    current_stage: StageName | None = None
    attempt_count: int = Field(ge=0)
    retry_cap: int = Field(ge=0)
    critic_threshold: float = Field(ge=0.0, le=1.0)
    failed_stage: StageName | None = None
    failed_reason: str | None = None
    submitted_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    trace: list[StageTraceEntry]
    output: Campaign | None = None
