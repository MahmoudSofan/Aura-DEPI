"""SC-006 / C1 — a ``done`` run is byte-identical after an API restart.

Spec acceptance: *"Status retrievable after API restart."* Concretely
we want to prove that once a campaign reaches ``done``, the SQLite +
filesystem state is the source of truth and another process bouncing
the API yields the same payload to the next ``GET /campaigns/{run_id}``
caller. The interrupt sweeper must NOT touch terminal rows when it runs
on the rebuilt engine.

The test stops short of forking a second process — it disposes the
SQLAlchemy engine, re-creates it against the same SQLite file, runs
the interrupt sweeper, and re-fetches the run via a fresh
``TestClient``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from backend.tests.conftest import (
    StubRecorder,
    make_copy_response,
    make_critic_response,
    make_pil_image,
)
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine


def _wait_for_done(client: TestClient, run_id: str, *, timeout_s: float = 60.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        body = client.get(f"/api/v1/campaigns/{run_id}").json()
        if body["status"] in ("done", "failed"):
            return dict(body)
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not finish in {timeout_s}s")


def test_done_run_survives_restart(
    api_client: TestClient,
    seeded_brand_id_via_client: str,
    stub_openai: StubRecorder,
    stub_image: StubRecorder,
    tmp_data_dir: Path,
    db_url: str,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Submit, wait for done, restart the engine, re-fetch — payload must match."""

    stub_openai.push(make_copy_response())
    stub_image.push(make_pil_image(width=512, height=512))
    stub_openai.push(make_critic_response())

    submit = api_client.post(
        "/api/v1/campaigns",
        json={
            "brief": "Survives restart.",
            "platform": "instagram",
            "brand_id": seeded_brand_id_via_client,
            "target_audience": "ya",
        },
    )
    assert submit.status_code == 202, submit.text
    run_id = submit.json()["run_id"]

    pre_restart = _wait_for_done(api_client, run_id)
    assert pre_restart["status"] == "done", pre_restart
    image_path = tmp_data_dir / "artifacts" / seeded_brand_id_via_client / f"{run_id}.png"
    assert image_path.exists(), "fixture pre-condition: artifact PNG was written"
    pre_image_bytes = image_path.read_bytes()

    # --- Simulate a restart: dispose and re-create the engine. ---------
    from backend.persistence import session as session_module
    from backend.persistence.session import create_engine_for_url

    session_module.engine.dispose()

    new_engine = create_engine_for_url(db_url)
    monkeypatch.setattr(session_module, "engine", new_engine)
    from sqlalchemy.orm import sessionmaker

    monkeypatch.setattr(
        session_module,
        "SessionLocal",
        sessionmaker(
            bind=new_engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            future=True,
        ),
    )

    # The sweeper runs on every boot — it must not touch terminal rows.
    from backend.orchestrator.interrupt_sweeper import run_interrupt_sweep

    run_interrupt_sweep()

    # --- Re-fetch the run; payload must be identical -------------------
    post_restart = api_client.get(f"/api/v1/campaigns/{run_id}").json()

    # status / output / trace are byte-stable across restart.
    assert post_restart["status"] == pre_restart["status"]
    assert post_restart["output"] == pre_restart["output"]
    assert post_restart["trace"] == pre_restart["trace"]
    assert post_restart["progress"] == pre_restart["progress"]
    assert post_restart["completed_at"] == pre_restart["completed_at"]
    assert post_restart["attempt_count"] == pre_restart["attempt_count"]

    # The artifact PNG is still on disk and byte-identical.
    assert image_path.exists()
    assert image_path.read_bytes() == pre_image_bytes

    # And it is still streamable through the API.
    img_resp = api_client.get(post_restart["output"]["image_url"])
    assert img_resp.status_code == 200
    assert img_resp.headers["content-type"] == "image/png"
    assert img_resp.content == pre_image_bytes
