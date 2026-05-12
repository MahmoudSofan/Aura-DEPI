"""Sentence-transformers wrapper for chunk embeddings (research.md §6).

Lazy-loads ``sentence-transformers/all-MiniLM-L6-v2`` (384-dim) on first
call. Tests that don't want to download model weights monkey-patch
:func:`_get_model` to return a deterministic stub.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

logger = logging.getLogger("aura.rag.embeddings")

_DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

_model_lock = threading.Lock()
_model_singleton: Any | None = None


def _model_name() -> str:
    return os.getenv("AURA_EMBEDDING_MODEL", _DEFAULT_MODEL)


def _get_model() -> Any:
    """Return the cached SentenceTransformer instance, loading on first call."""

    global _model_singleton
    if _model_singleton is not None:
        return _model_singleton
    with _model_lock:
        if _model_singleton is None:
            from sentence_transformers import SentenceTransformer

            logger.info("loading sentence-transformers model %s", _model_name())
            _model_singleton = SentenceTransformer(_model_name())
    return _model_singleton


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed a batch of strings, returning lists of floats.

    Empty input returns an empty list. Otherwise calls the underlying model
    once for the batch.
    """

    if not texts:
        return []

    model = _get_model()
    raw = model.encode(texts, show_progress_bar=False, convert_to_numpy=False)
    out: list[list[float]] = []
    for vec in raw:
        if hasattr(vec, "tolist"):
            out.append([float(x) for x in vec.tolist()])
        else:
            out.append([float(x) for x in vec])
    return out


__all__ = ["embed_batch"]
