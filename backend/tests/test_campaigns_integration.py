"""Full-pipeline happy-path integration tests for User Story 1.

Uses stubbed OpenRouter / Tavily clients so the test runs offline. Walks
through ``POST /api/v1/campaigns`` → polled ``GET /api/v1/campaigns/{id}``
until the run reaches ``status='done'`` and asserts the payload shape
documented in `contracts/openapi.yaml`.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

from backend.tests.conftest import (
    StubRecorder,
    make_copy_response,
    make_critic_response,
    make_pil_image,
)
from fastapi.testclient import TestClient


def _poll_until_terminal(
    api_client: TestClient,
    run_id: str,
    *,
    timeout_s: float | None = None,
) -> tuple[dict[str, Any], list[float], list[str]]:
    """Poll until status is done/failed. Returns (final, progresses, statuses)."""

    if timeout_s is None:
        timeout_s = float(os.getenv("AURA_E2E_TEST_BUDGET_S", "90"))
    deadline = time.monotonic() + timeout_s
    progresses: list[float] = []
    statuses: list[str] = []

    while time.monotonic() < deadline:
        resp = api_client.get(f"/api/v1/campaigns/{run_id}")
        assert resp.status_code == 200, resp.text
        body: dict[str, Any] = resp.json()
        progresses.append(float(body["progress"]))
        statuses.append(str(body["status"]))
        if body["status"] in ("done", "failed"):
            return body, progresses, statuses
        time.sleep(0.05)

    raise AssertionError(f"run {run_id} did not reach terminal in {timeout_s}s")


def test_happy_path(
    api_client: TestClient,
    seeded_brand_id_via_client: str,
    stub_openai: StubRecorder,
    stub_image: StubRecorder,
) -> None:
    """The full submit → poll → done flow with stubbed externals."""

    # Pipeline call order: copy (LLM) → image (chat-completions w/ image
    # modality) → critic (LLM). All three go through OpenRouter. Tavily is
    # unset → research degrades.
    stub_openai.push(make_copy_response())
    stub_image.push(make_pil_image(width=1024, height=1024))
    stub_openai.push(make_critic_response())

    submit_started = time.monotonic()
    submit = api_client.post(
        "/api/v1/campaigns",
        json={
            "brief": "Launch summer sneaker line.",
            "platform": "instagram",
            "brand_id": seeded_brand_id_via_client,
            "target_audience": "18-24 urban fitness-curious",
        },
    )
    submit_latency = time.monotonic() - submit_started
    assert submit.status_code == 202, submit.text
    submit_payload = submit.json()
    run_id = submit_payload["run_id"]
    assert submit_payload["status"] == "queued"
    # SC-002 light gate: submit returns under 1s comfortably.
    assert submit_latency < 5.0, f"submit took {submit_latency:.2f}s"

    final, progresses, _statuses = _poll_until_terminal(api_client, run_id)

    # Status / shape.
    assert final["status"] == "done", (
        f"final status={final['status']} "
        f"failed_stage={final.get('failed_stage')} "
        f"failed_reason={final.get('failed_reason')} "
        f"trace={[(t['stage'], t['status'], t.get('error_message')) for t in final.get('trace', [])]}"
    )
    assert final["output"] is not None, final
    output = final["output"]

    # ad_copy + image_url shape.
    assert output["ad_copy"]["headline"]
    assert output["ad_copy"]["primary_text"]
    assert output["ad_copy"]["cta"]
    assert re.match(r"^/api/v1/artifacts/[A-Za-z0-9]+/[A-Za-z0-9]+\.png$", output["image_url"]), (
        output["image_url"]
    )

    # Critic score has the four required dimensions.
    breakdown = output["score"]["breakdown"]
    for dim in ("relevance", "brand_fit", "clarity", "factual_alignment"):
        assert dim in breakdown
        assert 0.0 <= float(breakdown[dim]) <= 1.0

    # Trace has one entry per stage in the documented order.
    trace = final["trace"]
    stages = [entry["stage"] for entry in trace]
    assert stages == ["research", "retrieval", "copy", "image", "critic"], stages

    # Progress monotonicity.
    assert progresses == sorted(progresses), progresses
    assert progresses[-1] == 1.0

    # Per-stage model_calls observability (FR-019, C5).
    by_stage = {entry["stage"]: entry for entry in trace}
    assert len(by_stage["copy"]["model_calls"]) >= 1
    assert by_stage["copy"]["model_calls"][0]["provider"] == "openrouter"
    assert len(by_stage["critic"]["model_calls"]) >= 1
    assert by_stage["critic"]["model_calls"][0]["provider"] == "openrouter"
    assert len(by_stage["image"]["model_calls"]) >= 1
    assert by_stage["image"]["model_calls"][0]["provider"] == "openrouter"

    # Tavily isn't configured → research is degraded, no model calls.
    assert by_stage["research"]["status"] == "degraded"

    # End-to-end latency budget (SC-001 light gate).
    budget_s = float(os.getenv("AURA_E2E_TEST_BUDGET_S", "90"))
    assert budget_s > 0


def test_retry_path_end_to_end(
    api_client: TestClient,
    seeded_brand_id_via_client: str,
    stub_openai: StubRecorder,
    stub_image: StubRecorder,
) -> None:
    """US3 / T060: critic fails attempt 1 then passes attempt 2 via the API.

    The full pipeline executes twice (copy → image → critic), the run
    terminates with ``status='done'``, the persisted ``output.winning_attempt``
    points at the second attempt, and the trace exposes the per-attempt
    critic verdicts so an operator can audit the retry loop (FR-015, FR-016).
    """

    feedback_marker = "Lean harder into specific brand facts in the headline."

    # attempt 1: copy → image → critic (fail)
    stub_openai.push(make_copy_response(headline="Generic"))
    stub_image.push(make_pil_image(width=512, height=512))
    stub_openai.push(make_critic_response(overall=0.4, feedback=feedback_marker))
    # attempt 2: copy → image → critic (pass)
    stub_openai.push(make_copy_response(headline="Specific"))
    stub_image.push(make_pil_image(width=512, height=512))
    stub_openai.push(make_critic_response(overall=0.9, feedback="Better."))

    submit = api_client.post(
        "/api/v1/campaigns",
        json={
            "brief": "Brief",
            "platform": "instagram",
            "brand_id": seeded_brand_id_via_client,
            "target_audience": "ya",
        },
    )
    assert submit.status_code == 202, submit.text
    run_id = submit.json()["run_id"]

    final, _progresses, _statuses = _poll_until_terminal(api_client, run_id)
    assert final["status"] == "done", final
    assert final["attempt_count"] == 2, final

    output = final["output"]
    assert output is not None, final
    assert output["winning_attempt"] == 2, output
    assert output["score"]["passed"] is True
    assert output["score"]["overall"] >= 0.7

    # Two trace entries each for copy / image / critic — one per attempt.
    trace = final["trace"]
    by_stage_attempt: dict[str, list[int]] = {}
    for entry in trace:
        by_stage_attempt.setdefault(entry["stage"], []).append(entry["attempt"])
    for stage in ("copy", "image", "critic"):
        assert sorted(by_stage_attempt[stage]) == [1, 2], (stage, by_stage_attempt[stage])
    # Research / retrieval run only on attempt 1 — re-used across retries.
    assert by_stage_attempt["research"] == [1]
    assert by_stage_attempt["retrieval"] == [1]

    # Critic attempt-1 verdict is captured in the trace alongside the winning verdict.
    critic_entries = sorted(
        (e for e in trace if e["stage"] == "critic"), key=lambda e: e["attempt"]
    )
    assert critic_entries[0]["verdict"] is not None
    assert critic_entries[0]["verdict"]["passed"] is False
    assert critic_entries[1]["verdict"]["passed"] is True

    # Attempt-2 copy + image LLM calls received the prior critic's feedback.
    second_copy_user = next(
        m["content"]
        for c in stub_openai.calls
        if any("copywriter" in m.get("content", "") for m in c.get("messages", []))
        for m in c["messages"]
        if m.get("role") == "user" and feedback_marker in m.get("content", "")
    )
    assert feedback_marker in second_copy_user
    assert any(feedback_marker in c["messages"][0]["content"] for c in stub_image.calls[1:])
