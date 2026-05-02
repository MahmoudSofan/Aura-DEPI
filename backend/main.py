from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from agents.schemas import CampaignRequest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("aura.api")

app = FastAPI(title="Aura API", version="0.1.0")

UPLOAD_DIR = Path(__file__).parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

RUNS: dict[str, dict[str, Any]] = {}


def _ingest_document(doc_id: str, path: Path) -> None:
    # TODO: replace with Yousef's RAG ingest module once available.
    logger.info("[stub] ingesting doc_id=%s path=%s", doc_id, path)


async def _run_campaign(run_id: str, request: CampaignRequest) -> None:
    RUNS[run_id]["status"] = "running"
    logger.info("[stub] campaign run %s started; LangGraph pipeline not yet wired", run_id)
    # TODO: invoke agents.graph.run(request) once the LangGraph is implemented;
    # update RUNS[run_id] with progress / result / errors as it executes.


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    logger.warning(
        "%s %s -> %d %s", request.method, request.url.path, exc.status_code, exc.detail
    )
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


@app.post("/api/documents/upload")
async def upload_document(file: UploadFile = File(...)) -> dict[str, str]:
    doc_id = str(uuid.uuid4())
    suffix = Path(file.filename or "").suffix
    saved_path = UPLOAD_DIR / f"{doc_id}{suffix}"
    contents = await file.read()
    saved_path.write_bytes(contents)
    logger.info(
        "saved upload filename=%s -> %s (%d bytes)", file.filename, saved_path, len(contents)
    )
    _ingest_document(doc_id, saved_path)
    return {"doc_id": doc_id}


@app.post("/api/campaigns/generate", status_code=202)
async def generate_campaign(
    request: CampaignRequest, background_tasks: BackgroundTasks
) -> dict[str, str]:
    run_id = str(uuid.uuid4())
    RUNS[run_id] = {"status": "pending", "progress": 0.0, "result": None}
    background_tasks.add_task(_run_campaign, run_id, request)
    logger.info("accepted campaign request brand_id=%s run_id=%s", request.brand_id, run_id)
    return {"run_id": run_id}


@app.get("/api/campaigns/{run_id}/status")
async def get_campaign_status(run_id: str) -> dict[str, Any]:
    state = RUNS.get(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"run_id {run_id!r} not found")
    response: dict[str, Any] = {
        "run_id": run_id,
        "status": state["status"],
        "progress": state["progress"],
    }
    if state.get("result") is not None:
        response["result"] = state["result"]
    return response
