from __future__ import annotations

# Load .env before any other imports so module-level code in transitively
# imported modules (e.g. SQLAlchemy engine in backend/persistence/session.py)
# sees the configured env vars.
from dotenv import load_dotenv

load_dotenv()

import logging  # noqa: E402
from collections.abc import AsyncIterator  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402

from backend.api.artifacts import router as artifacts_router  # noqa: E402
from backend.api.brands import router as brands_router  # noqa: E402
from backend.api.campaigns import router as campaigns_router  # noqa: E402
from backend.api.documents import router as documents_router  # noqa: E402
from backend.api.healthz import router as healthz_router  # noqa: E402
from backend.orchestrator.interrupt_sweeper import run_interrupt_sweep  # noqa: E402
from backend.orchestrator.runner import CampaignRunner  # noqa: E402
from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import JSONResponse, RedirectResponse  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("aura.api")


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # FR-025: fail any non-terminal run left over from a prior process.
    run_interrupt_sweep()

    # Construct + start the campaign runner.
    runner = CampaignRunner()
    app.state.campaign_runner = runner
    await runner.start()

    try:
        yield
    finally:
        await runner.stop()


app = FastAPI(title="Aura API", version="1.0.0", lifespan=_lifespan)


# ---------------------------------------------------------------------------
# Exception handlers.
# ---------------------------------------------------------------------------


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    logger.warning("%s %s -> %d %s", request.method, request.url.path, exc.status_code, exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    logger.warning(
        "%s %s -> 422 validation error: %s", request.method, request.url.path, exc.errors()
    )
    return JSONResponse(
        status_code=422,
        content={"detail": "Invalid request payload", "errors": exc.errors()},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


# ---------------------------------------------------------------------------
# Versioned routers.
# ---------------------------------------------------------------------------

app.include_router(healthz_router, prefix="/api/v1")
app.include_router(brands_router, prefix="/api/v1")
app.include_router(documents_router, prefix="/api/v1")
app.include_router(campaigns_router, prefix="/api/v1")
app.include_router(artifacts_router, prefix="/api/v1")


# ---------------------------------------------------------------------------
# Legacy unversioned routes — redirect / 410 per `research.md §13`.
# ---------------------------------------------------------------------------


@app.post("/api/campaigns/generate", include_in_schema=False)
async def _legacy_campaigns_generate() -> RedirectResponse:
    logger.info("legacy POST /api/campaigns/generate -> 308 /api/v1/campaigns")
    return RedirectResponse(url="/api/v1/campaigns", status_code=308)


@app.get("/api/campaigns/{run_id}/status", include_in_schema=False)
async def _legacy_campaigns_status(run_id: str) -> RedirectResponse:
    logger.info("legacy GET /api/campaigns/%s/status -> 308 /api/v1/campaigns/%s", run_id, run_id)
    return RedirectResponse(url=f"/api/v1/campaigns/{run_id}", status_code=308)


@app.post("/api/documents/upload", include_in_schema=False)
async def _legacy_documents_upload() -> JSONResponse:
    logger.info("legacy POST /api/documents/upload -> 410 Gone")
    return JSONResponse(
        status_code=410,
        content={
            "detail": (
                "POST /api/documents/upload has been removed. Documents are now "
                "scoped to a brand — use POST /api/v1/brands/{brand_id}/documents "
                "instead."
            ),
            "code": "legacy_route_gone",
        },
    )
