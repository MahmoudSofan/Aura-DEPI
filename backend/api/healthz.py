"""``GET /api/v1/healthz`` — liveness + dependency reachability.

Returns 200 ``HealthOk`` when SQLite is openable and ChromaDB heartbeat
succeeds. Returns 503 ``HealthDegraded`` otherwise, with each dependency's
state listed individually.
"""

from __future__ import annotations

import logging
import os
from typing import Literal

from backend.persistence.session import engine
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

logger = logging.getLogger("aura.api.healthz")

router = APIRouter(tags=["ops"])

_API_VERSION = "1.0.0"


class HealthOk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    version: str = _API_VERSION
    dependencies: dict[str, Literal["ok"]]


class HealthDegraded(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["degraded"] = "degraded"
    version: str = _API_VERSION
    dependencies: dict[str, Literal["ok", "unreachable"]]


def _check_sqlite() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # pragma: no cover - exercised in degraded tests
        logger.warning("sqlite health check failed: %s", exc)
        return False


def _check_chromadb() -> bool:
    host = os.getenv("CHROMA_HOST")
    port_raw = os.getenv("CHROMA_PORT")
    if not host or not port_raw:
        # Local dev without a running Chroma — report unreachable rather than
        # crashing the API. A future "embedded mode" toggle could be added.
        logger.info("chromadb env not configured (CHROMA_HOST/CHROMA_PORT)")
        return False
    try:
        port = int(port_raw)
    except ValueError:
        logger.warning("CHROMA_PORT=%r is not an integer", port_raw)
        return False
    try:
        import chromadb

        client = chromadb.HttpClient(host=host, port=port)
        client.heartbeat()
        return True
    except Exception as exc:  # pragma: no cover - exercised in degraded tests
        logger.warning("chromadb heartbeat failed: %s", exc)
        return False


@router.get(
    "/healthz",
    summary="Liveness + dependency health",
    operation_id="healthz",
    responses={
        200: {"model": HealthOk},
        503: {"model": HealthDegraded},
    },
)
def healthz() -> JSONResponse:
    sqlite_ok = _check_sqlite()
    chroma_ok = _check_chromadb()

    if sqlite_ok and chroma_ok:
        body = HealthOk(dependencies={"sqlite": "ok", "chromadb": "ok"})
        return JSONResponse(status_code=200, content=body.model_dump())

    body_degraded = HealthDegraded(
        dependencies={
            "sqlite": "ok" if sqlite_ok else "unreachable",
            "chromadb": "ok" if chroma_ok else "unreachable",
        }
    )
    return JSONResponse(status_code=503, content=body_degraded.model_dump())


__all__ = ["router", "HealthOk", "HealthDegraded"]
