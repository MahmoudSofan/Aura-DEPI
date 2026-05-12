"""FastAPI routers for the `/api/v1` surface.

Each resource lives in its own module (``brands``, ``documents``,
``campaigns``, ``artifacts``, ``healthz``) and exposes a ``router``
constant. :mod:`backend.main` registers them under the ``/api/v1`` prefix.
"""

from __future__ import annotations

from backend.api.healthz import router as healthz_router

__all__ = ["healthz_router"]
