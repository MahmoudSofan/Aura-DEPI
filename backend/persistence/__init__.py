"""Persistence layer: SQLAlchemy 2.x ORM + Alembic for SQLite.

Public surface:

* :class:`Base` — declarative base for ORM models (re-exported from
  :mod:`backend.persistence.models`).
* :func:`get_session` — FastAPI dependency yielding a scoped Session.
* :func:`session_scope` — context manager for non-FastAPI callers
  (orchestrator, scripts, tests).
"""

from __future__ import annotations

from backend.persistence.session import (
    SessionLocal,
    create_engine_for_url,
    engine,
    get_session,
    resolve_database_url,
    session_scope,
)

__all__ = [
    "SessionLocal",
    "create_engine_for_url",
    "engine",
    "get_session",
    "resolve_database_url",
    "session_scope",
]
