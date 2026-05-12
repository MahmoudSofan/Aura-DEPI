"""US3 / T054: critic retry passes on the second attempt.

Stubs the LLM and image clients so the critic returns ``passed=False`` on
attempt 1 and ``passed=True`` on attempt 2. Asserts the graph re-enters
``copy`` for a second attempt, the second attempt's copy/image LLM calls
embed the previous critic's feedback, and the run terminates with the
second attempt as the chosen winner (FR-015, FR-016).
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


def _request(brand_id: str = "01HXBRAND0000000000000000") -> CampaignRequest:
    return CampaignRequest(
        brief="Brief",
        platform="instagram",
        brand_id=brand_id,
        target_audience="ya",
    )


def _ephemeral_chroma() -> Any:
    import chromadb
    from chromadb.config import Settings

    client = chromadb.Client(Settings(is_persistent=False, allow_reset=True))
    client.reset()
    return client


def test_critic_retry_pass_on_second(
    tmp_path: Path,
    stub_openai: StubRecorder,
    stub_image: StubRecorder,
) -> None:
    feedback_marker = "Heading is too generic — call out the specific brand promise."

    # Order of LLM consumption:
    #   attempt 1: copy (LLM) → image → critic (LLM, fail)
    #   attempt 2: copy (LLM) → image → critic (LLM, pass)
    stub_openai.push(make_copy_response(headline="Generic headline"))
    stub_image.push(make_pil_image())
    stub_openai.push(make_critic_response(overall=0.4, feedback=feedback_marker))
    stub_openai.push(make_copy_response(headline="Sharper specific headline"))
    stub_image.push(make_pil_image())
    stub_openai.push(make_critic_response(overall=0.85, feedback="Much better."))

    captured: list[StageTraceEntry] = []

    async def on_event(entry: StageTraceEntry) -> None:
        captured.append(entry)

    attempts, failed_stage, failed_reason = asyncio.run(
        graph_run(
            _request(),
            run_id="01HXRUN000000000000000000",
            retry_cap=2,
            critic_threshold=0.7,
            artifacts_dir=tmp_path,
            chroma_client=_ephemeral_chroma(),
            on_stage_event=on_event,
        )
    )

    assert failed_stage is None, (failed_stage, failed_reason)
    assert attempts is not None and len(attempts) == 2

    # Trace contains entries for both attempts of copy / image / critic.
    by_attempt_stage = {(e.attempt, e.stage) for e in captured}
    for stage in ("copy", "image", "critic"):
        assert (1, stage) in by_attempt_stage, (stage, by_attempt_stage)
        assert (2, stage) in by_attempt_stage, (stage, by_attempt_stage)
    # Research and retrieval are computed once and re-used across attempts.
    research_attempts = [e.attempt for e in captured if e.stage == "research"]
    retrieval_attempts = [e.attempt for e in captured if e.stage == "retrieval"]
    assert research_attempts == [1], research_attempts
    assert retrieval_attempts == [1], retrieval_attempts

    # The second-attempt copy + image calls received attempt-1's critic feedback.
    copy_calls = [
        c
        for c in stub_openai.calls
        if any(
            isinstance(m, dict)
            and m.get("role") == "system"
            and "copywriter" in m.get("content", "")
            for m in c.get("messages", [])
        )
    ]
    assert len(copy_calls) == 2, copy_calls
    second_copy_user_msg = next(
        m["content"] for m in copy_calls[1]["messages"] if m.get("role") == "user"
    )
    assert feedback_marker in second_copy_user_msg, second_copy_user_msg

    second_image_call = stub_image.calls[1]
    second_image_prompt = second_image_call["messages"][0]["content"]
    assert feedback_marker in second_image_prompt, second_image_prompt

    # Winner is attempt 2 (the higher score) with passed=True.
    winner = max(attempts, key=lambda a: a.critic.overall)
    assert winner.attempt == 2
    assert winner.critic.passed is True
    assert winner.critic.overall == pytest.approx(0.85)
