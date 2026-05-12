"""Artifacts API — stream generated PNG images.

Bytes are *never* inlined in the JSON status payload (FR-023). Operators
fetch the artifact by URL via this endpoint, which validates the brand
exists and rejects any path-traversal characters in the filename.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from backend.persistence.repository import BrandRepository
from backend.persistence.session import get_session
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

logger = logging.getLogger("aura.api.artifacts")

router = APIRouter(tags=["artifacts"])

_FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+\.png$")


def _artifacts_root() -> Path:
    return Path(os.getenv("AURA_DATA_DIR", "backend/data")) / "artifacts"


@router.get(
    "/artifacts/{brand_id}/{filename}",
    summary="Stream a generated PNG image artifact",
    operation_id="getArtifact",
    responses={
        200: {"content": {"image/png": {}}},
        400: {"description": "Filename contains path-traversal characters"},
        404: {"description": "Brand or artifact not found"},
    },
)
def get_artifact(
    brand_id: str,
    filename: str,
    session: Session = Depends(get_session),
) -> FileResponse:
    if any(ch in filename for ch in ("/", "\\", "..")) or not _FILENAME_PATTERN.match(filename):
        raise HTTPException(status_code=400, detail="invalid filename")

    brand = BrandRepository.get(session, brand_id)
    if brand is None:
        raise HTTPException(status_code=404, detail=f"brand_id {brand_id!r} not found")

    root = _artifacts_root().resolve()
    candidate = (root / brand_id / filename).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        logger.warning("rejected artifact path outside root: %s", candidate)
        raise HTTPException(status_code=404, detail="artifact not found") from exc

    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="artifact not found")

    return FileResponse(path=candidate, media_type="image/png", filename=filename)


__all__ = ["router"]
