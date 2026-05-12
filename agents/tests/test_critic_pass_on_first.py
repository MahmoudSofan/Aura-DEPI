"""US3 / T056: critic passes on the first attempt — no retry runs."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from agents.graph import run as graph_run
from agents.schemas import CampaignRequest, StageTraceEntry
from backend.tests.conftest import (
    StubRecorder,
    make_copy_response,
    make_critic_response,
    make_pil_image,
)


def _ephemeral_chroma() -> Any:
    import chromadb
    from chromadb.config import Settings

    client = chromadb.Client(Settings(is_persistent=False, allow_reset=True))
    client.reset()
    return client


def test_critic_pass_on_first_no_retry(
    tmp_path: Path,
    stub_openai: StubRecorder,
    stub_image: StubRecorder,
) -> None:
    stub_openai.push(make_copy_response())
    stub_image.push(make_pil_image())
    stub_openai.push(make_critic_response(overall=0.95))

    captured: list[StageTraceEntry] = []

    async def on_event(entry: StageTraceEntry) -> None:
        captured.append(entry)

    request = CampaignRequest(
        brief="Brief", platform="instagram", brand_id="01HXBRAND", target_audience="ya"
    )

    attempts, failed_stage, failed_reason = asyncio.run(
        graph_run(
            request,
            run_id="01HXRUN000000000000000000",
            retry_cap=2,
            critic_threshold=0.7,
            artifacts_dir=tmp_path,
            chroma_client=_ephemeral_chroma(),
            on_stage_event=on_event,
        )
    )

    assert failed_stage is None, (failed_stage, failed_reason)
    assert attempts is not None and len(attempts) == 1

    # Exactly one of each retry-eligible stage (no second copy/image/critic call).
    for stage in ("copy", "image", "critic"):
        instances = [e for e in captured if e.stage == stage]
        assert len(instances) == 1, (stage, instances)

    winner = attempts[0]
    assert winner.attempt == 1
    assert winner.critic.passed is True
