"""SQLAlchemy engine + session factory for Aura.

The default database lives at ``${AURA_DATA_DIR}/aura.db`` (default
``backend/data/aura.db``). WAL journal mode is enabled at connection time,
and ``PRAGMA foreign_keys = ON`` is set so the FR-024 cascade actually
fires on SQLite.
"""

from __future__ import annotations

import os
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine as _sa_create_engine
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.declarative import DeclarativeMeta
from sqlalchemy.orm import Session, sessionmaker

_DEFAULT_DATA_DIR = "backend/data"
_DEFAULT_DB_FILENAME = "aura.db"


def resolve_database_url() -> str:
    """Return the SQLAlchemy URL for the configured SQLite database.

    Honours ``AURA_DATABASE_URL`` (full SQLAlchemy URL) when set; otherwise
    composes ``sqlite:///${AURA_DATA_DIR}/aura.db``. The parent directory is
    created if it doesn't already exist.
    """

    explicit = os.getenv("AURA_DATABASE_URL")
    if explicit:
        return explicit

    data_dir = Path(os.getenv("AURA_DATA_DIR", _DEFAULT_DATA_DIR))
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / _DEFAULT_DB_FILENAME
    return f"sqlite:///{db_path.as_posix()}"


def create_engine_for_url(url: str, *, echo: bool = False) -> Engine:
    """Build a SQLAlchemy engine with the right SQLite pragmas wired in."""

    connect_args: dict[str, Any] = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False

    engine_obj = _sa_create_engine(url, echo=echo, future=True, connect_args=connect_args)

    if url.startswith("sqlite"):

        @event.listens_for(engine_obj, "connect")
        def _set_sqlite_pragmas(  # type: ignore[no-untyped-def]
            dbapi_connection, connection_record
        ):
            cur = dbapi_connection.cursor()
            cur.execute("PRAGMA foreign_keys = ON")
            cur.execute("PRAGMA journal_mode = WAL")
            cur.execute("PRAGMA synchronous = NORMAL")
            cur.close()

    return engine_obj


engine: Engine = create_engine_for_url(resolve_database_url())

SessionLocal: sessionmaker[Session] = sessionmaker(
    bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True
)


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a SQLAlchemy ``Session``.

    Usage::

        @router.get("/...")
        def handler(session: Session = Depends(get_session)) -> ...: ...

    Rolls back on uncaught exception and always closes the session.
    """

    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context manager equivalent of :func:`get_session` for non-FastAPI code."""

    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# Re-exported for ergonomic ``from backend.persistence import Base``.
# The actual class lives in :mod:`backend.persistence.models`; importing it
# here would create a circular import, so callers that need ``Base`` should
# import it directly from the models module.
__all__ = [
    "DeclarativeMeta",
    "Engine",
    "Session",
    "SessionLocal",
    "create_engine_for_url",
    "engine",
    "get_session",
    "resolve_database_url",
    "session_scope",
]
