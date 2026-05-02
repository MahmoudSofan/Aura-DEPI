from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Platform = Literal["facebook", "instagram", "tiktok", "twitter", "linkedin", "youtube"]


class CampaignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brief: str = Field(min_length=1)
    platform: Platform
    brand_id: str = Field(min_length=1)
    target_audience: str = Field(min_length=1)


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
    copy: AdCopy
    image: GeneratedImage
    score: CriticScore
    run_id: str = Field(min_length=1)
