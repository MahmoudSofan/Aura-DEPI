"""Brand-grounded copy integration test (T045, FR-012, partial SC-003).

Ingest a document containing a unique invented marker phrase, submit a
brief targeting that brand, and assert the delivered ad copy contains
tokens drawn from the document. The copy LLM is stubbed to echo the
retrieved chunks back into the headline and primary text — the spirit
of the test is that retrieval surfaces the right brand chunks to the
copy stage's prompt, not that a real LLM grounds itself.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any

import pytest
from backend.tests.conftest import StubRecorder, make_critic_response, make_pil_image
from fastapi.testclient import TestClient


def _create_brand(api_client: TestClient, display_name: str = "Grounded Brand") -> str:
    return str(api_client.post("/api/v1/brands", json={"display_name": display_name}).json()["id"])


def _wait_for_done(
    api_client: TestClient, run_id: str, *, timeout_s: float = 60.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        body = api_client.get(f"/api/v1/campaigns/{run_id}").json()
        if body["status"] in ("done", "failed"):
            return dict(body)
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not finish in {timeout_s}s")


def test_copy_references_brand_facts(
    api_client: TestClient,
    stub_embeddings: None,
    stub_image: StubRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bid = _create_brand(api_client, "ACME Footwear")

    marker_phrase = "ACME Mesh Pro Free shipping over $75"
    payload = (
        f"Brand voice: confident, technical.\n"
        f"{marker_phrase}.\n"
        f"All shoes ship next day in the contiguous US."
    ).encode()

    upload = api_client.post(
        f"/api/v1/brands/{bid}/documents",
        files={"file": ("brand.txt", payload, "text/plain")},
    )
    assert upload.status_code == 201, upload.text

    # Echoing copy stub: pull the retrieved chunks out of the user prompt and
    # parrot the marker back into the headline and primary text. This is the
    # operational stand-in for "the LLM uses the retrieved facts."
    def echoing_copy(**kwargs: Any) -> Any:
        messages = kwargs.get("messages") or []
        joined = "\n".join(m.get("content", "") for m in messages)
        # Grab the marker phrase verbatim if it appears in the user prompt
        # (which it should, since retrieval surfaced the chunk above).
        echo = marker_phrase if marker_phrase in joined else "Generic Headline"
        content = json.dumps(
            {
                "headline": echo,
                "primary_text": f"From our brand brief: {echo}.",
                "cta": "Shop now",
                "platform": "instagram",
            }
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=42, completion_tokens=24),
        )

    fake_copy_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=echoing_copy))
    )
    monkeypatch.setattr("agents.stages.copy._make_llm_client", lambda: fake_copy_client)

    # The critic still uses the conftest's stub_openai (the same module-level
    # patch on agents.stages.critic._make_llm_client). Push a passing critic
    # response and an image.
    fake_critic_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **kwargs: SimpleNamespace(
                    choices=[
                        SimpleNamespace(message=SimpleNamespace(content=make_critic_response()))
                    ],
                    usage=SimpleNamespace(prompt_tokens=50, completion_tokens=30),
                )
            )
        )
    )
    monkeypatch.setattr("agents.stages.critic._make_llm_client", lambda: fake_critic_client)
    stub_image.push(make_pil_image(width=64, height=64))

    submit = api_client.post(
        "/api/v1/campaigns",
        json={
            "brief": "Promote our shoes to fitness-curious city dwellers.",
            "platform": "instagram",
            "brand_id": bid,
            "target_audience": "18-24 fitness-curious",
        },
    )
    assert submit.status_code == 202, submit.text
    run_id = submit.json()["run_id"]

    final = _wait_for_done(api_client, run_id)
    assert final["status"] == "done", (
        f"final status={final['status']} "
        f"failed_stage={final.get('failed_stage')} "
        f"failed_reason={final.get('failed_reason')}"
    )

    output = final["output"]
    blob = (output["ad_copy"]["headline"] + " " + output["ad_copy"]["primary_text"]).lower()
    assert "acme" in blob, output["ad_copy"]
    assert "mesh pro" in blob.lower(), output["ad_copy"]
