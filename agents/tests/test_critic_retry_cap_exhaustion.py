"""US3 / T055: critic exhausts the retry cap; best-scoring attempt wins.

The critic always returns ``passed=False`` with monotonically-improving
overall scores ``[0.4, 0.5, 0.6]``. The graph runs ``retry_cap + 1 = 3``
attempts then terminates. The runner picks the highest-scoring attempt as
winner; the run lands in ``status='done'`` with ``passed=False`` (FR-016).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
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


def test_retry_cap_exhausted_best_attempt_wins(
    tmp_path: Path,
    stub_openai: StubRecorder,
    stub_image: StubRecorder,
) -> None:
    retry_cap = 2  # → 3 attempts total
    overalls = [0.4, 0.5, 0.6]

    for score in overalls:
        stub_openai.push(make_copy_response())
        stub_image.push(make_pil_image())
        stub_openai.push(make_critic_response(overall=score, feedback="needs work"))

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
            retry_cap=retry_cap,
            critic_threshold=0.7,
            artifacts_dir=tmp_path,
            chroma_client=_ephemeral_chroma(),
            on_stage_event=on_event,
        )
    )

    assert failed_stage is None, (failed_stage, failed_reason)
    assert attempts is not None and len(attempts) == retry_cap + 1

    # Trace covers all 3 critic attempts and never exceeds the cap.
    critic_attempts = sorted(e.attempt for e in captured if e.stage == "critic")
    assert critic_attempts == [1, 2, 3]

    # Highest-scoring attempt wins (the third with overall=0.6).
    winner = max(attempts, key=lambda a: a.critic.overall)
    assert winner.attempt == 3
    assert winner.critic.overall == pytest.approx(overalls[-1])
    assert winner.critic.passed is False  # 0.6 < 0.7 threshold
