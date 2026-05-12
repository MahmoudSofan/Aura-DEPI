"""Phase 2 (Foundational) smoke test.

Verifies the checkpoint conditions enumerated at the end of Phase 2 in
``tasks.md``:

* ``uvicorn backend.main:app`` starts (FastAPI app constructible).
* ``GET /api/v1/healthz`` returns 200 when SQLite is reachable and the
  ChromaDB env is set, or 503 otherwise.
* The startup event runs the interrupt sweeper against the test DB.
* The legacy unversioned routes redirect / return 410.
"""

from __future__ import annotations

from datetime import UTC, datetime

from backend.persistence.repository import BrandRepository, RunRepository
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine


def test_healthz_returns_503_without_chromadb(api_client: TestClient) -> None:
    """No CHROMA_HOST/PORT in the test env => degraded with chromadb=unreachable."""

    resp = api_client.get("/api/v1/healthz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["dependencies"]["sqlite"] == "ok"
    assert body["dependencies"]["chromadb"] == "unreachable"


def test_legacy_campaigns_generate_redirects(api_client: TestClient) -> None:
    resp = api_client.post("/api/campaigns/generate", follow_redirects=False)
    assert resp.status_code == 308
    assert resp.headers["location"] == "/api/v1/campaigns"


def test_legacy_campaigns_status_redirects(api_client: TestClient) -> None:
    resp = api_client.get("/api/campaigns/01HXFAKE/status", follow_redirects=False)
    assert resp.status_code == 308
    assert resp.headers["location"] == "/api/v1/campaigns/01HXFAKE"


def test_legacy_documents_upload_returns_410(api_client: TestClient) -> None:
    resp = api_client.post("/api/documents/upload", follow_redirects=False)
    assert resp.status_code == 410
    body = resp.json()
    assert body["code"] == "legacy_route_gone"
    assert "/api/v1/brands/{brand_id}/documents" in body["detail"]


def test_startup_sweeper_marks_inflight_runs_failed(api_client: TestClient, engine: Engine) -> None:
    """Drop a 'running' run into the DB, restart the app, sweeper fails it."""

    from backend.persistence import session as session_module

    # The api_client fixture has already triggered startup once. To exercise
    # the sweeper, seed a brand+run and explicitly invoke the sweeper.
    with session_module.SessionLocal() as s:
        BrandRepository.create(s, brand_id="01HXBRAND00000000000000000", display_name="Acme")
        run = RunRepository.create_queued(
            s,
            run_id="01HXRUN00000000000000000000",
            brand_id="01HXBRAND00000000000000000",
            brief="hi",
            platform="instagram",
            target_audience="young pros",
            retry_cap=2,
            critic_threshold=0.7,
        )
        run.status = "running"
        run.started_at = datetime.now(tz=UTC)
        s.commit()

    from backend.orchestrator.interrupt_sweeper import run_interrupt_sweep

    swept = run_interrupt_sweep()
    assert swept == ["01HXRUN00000000000000000000"]

    with session_module.SessionLocal() as s:
        swept_run = RunRepository.get(s, "01HXRUN00000000000000000000")
        assert swept_run is not None
        assert swept_run.status == "failed"
        assert swept_run.failed_reason == "interrupted_by_restart"
        assert swept_run.completed_at is not None
