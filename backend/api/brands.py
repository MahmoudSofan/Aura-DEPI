"""Brands API — create / list / get / delete with cascading hard-delete (FR-024).

Delete order (per data-model.md §"Filesystem layout" and the FR-024 cascade):

1. Mark every non-terminal run for the brand as ``failed`` with
   ``failed_reason='brand_deleted'`` so any in-flight runner observing the
   row sees a terminal state. Commit.
2. Run the SQL cascade (``BrandRepository.delete``) which removes the
   brand row and all FK-dependent rows (documents, runs, stage_traces,
   campaign_outputs).
3. Best-effort delete the Chroma collection ``brand_{brand_id}``.
4. Best-effort recursively remove ``backend/data/uploads/{brand_id}/`` and
   ``backend/data/artifacts/{brand_id}/``.

Steps 3 and 4 are idempotent and never roll back the SQL transaction —
orphaned files left by a crash are reaped by a later brand-delete or by a
startup sweep.
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from agents.schemas import Brand
from backend.persistence.repository import BrandRepository, RunRepository
from backend.persistence.session import get_session
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from ulid import ULID

logger = logging.getLogger("aura.api.brands")

router = APIRouter(tags=["brands"])


# ---------------------------------------------------------------------------
# Request shapes.
# ---------------------------------------------------------------------------


class BrandCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=200)


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _data_root() -> Path:
    return Path(os.getenv("AURA_DATA_DIR", "backend/data"))


def _to_schema(brand: Any) -> Brand:
    return Brand(
        id=cast(str, brand.id),
        display_name=cast(str, brand.display_name),
        created_at=cast(datetime, brand.created_at),
        updated_at=cast(datetime, brand.updated_at),
    )


def _drop_chroma_collection(brand_id: str, request: Request) -> None:
    """Idempotent delete of ``brand_{brand_id}`` from Chroma."""

    runner = getattr(request.app.state, "campaign_runner", None)
    factory = getattr(runner, "_chroma_factory", None) if runner is not None else None
    if factory is None:
        logger.info("brand %s: no chroma factory available; skipping collection drop", brand_id)
        return
    try:
        client = factory()
        client.delete_collection(name=f"brand_{brand_id}")
        logger.info("brand %s: chroma collection dropped", brand_id)
    except Exception as exc:  # noqa: BLE001
        # Idempotent — "not found" is the common case.
        logger.info("brand %s: chroma collection drop skipped/failed: %s", brand_id, exc)


def _drop_brand_directories(brand_id: str) -> None:
    """Best-effort recursive removal of per-brand uploads + artifacts dirs."""

    root = _data_root()
    for sub in ("uploads", "artifacts"):
        path = root / sub / brand_id
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            logger.info("brand %s: removed %s", brand_id, path)


# ---------------------------------------------------------------------------
# Routes.
# ---------------------------------------------------------------------------


@router.post(
    "/brands",
    status_code=status.HTTP_201_CREATED,
    response_model=Brand,
    summary="Create a brand",
    operation_id="createBrand",
)
def create_brand(
    body: BrandCreateRequest,
    session: Session = Depends(get_session),
) -> Brand:
    brand_id = str(ULID())
    brand = BrandRepository.create(session, brand_id=brand_id, display_name=body.display_name)
    session.commit()
    logger.info("brand created id=%s", brand_id)
    return _to_schema(brand)


@router.get(
    "/brands",
    response_model=list[Brand],
    summary="List brands (newest first)",
    operation_id="listBrands",
)
def list_brands(session: Session = Depends(get_session)) -> list[Brand]:
    return [_to_schema(b) for b in BrandRepository.list_brands(session)]


@router.get(
    "/brands/{brand_id}",
    response_model=Brand,
    summary="Fetch a brand",
    operation_id="getBrand",
)
def get_brand(
    brand_id: str,
    session: Session = Depends(get_session),
) -> Brand:
    brand = BrandRepository.get(session, brand_id)
    if brand is None:
        raise HTTPException(status_code=404, detail=f"brand_id {brand_id!r} not found")
    return _to_schema(brand)


@router.delete(
    "/brands/{brand_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a brand and all its data (cascading)",
    operation_id="deleteBrand",
)
def delete_brand(
    brand_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> None:
    """Cascading delete per FR-024."""

    brand = BrandRepository.get(session, brand_id)
    if brand is None:
        raise HTTPException(status_code=404, detail=f"brand_id {brand_id!r} not found")

    # Step 1: mark non-terminal runs for this brand as failed so any in-flight
    # runner observes a terminal state before the cascade removes the row.
    inflight = RunRepository.list_runs(session, brand_id=brand_id, limit=200)
    cancelled = 0
    for run in inflight:
        if run.status in ("queued", "running"):
            RunRepository.transition_to_failed(
                session,
                run.id,
                failed_stage=None,
                failed_reason="brand_deleted",
            )
            cancelled += 1
    if cancelled:
        session.commit()
        logger.info("brand %s: marked %d non-terminal run(s) as failed", brand_id, cancelled)

    # Step 2: SQL cascade.
    BrandRepository.delete(session, brand_id)
    session.commit()
    logger.info("brand %s: SQL cascade complete", brand_id)

    # Step 3 + 4: best-effort cleanup outside the SQL transaction.
    _drop_chroma_collection(brand_id, request)
    _drop_brand_directories(brand_id)


__all__ = ["router", "BrandCreateRequest"]
