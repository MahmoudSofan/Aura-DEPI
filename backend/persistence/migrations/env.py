"""Alembic migration environment for Aura.

The `target_metadata` import is a forward reference: T008 lands
`backend/persistence/models.py` with the SQLAlchemy `Base`.  Until then,
`alembic upgrade` will fail with an ImportError — this is intentional and
unblocks generating the initial migration once T008 is merged.
"""

from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from backend.persistence.models import Base  # noqa: F401  (forward reference, T008)
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Allow callers to override the SQLAlchemy URL via AURA_DATABASE_URL — useful
# for autogenerate against a temporary DB and for CI runs that want to avoid
# touching backend/data/aura.db.
_override_url = os.getenv("AURA_DATABASE_URL")
if _override_url:
    config.set_main_option("sqlalchemy.url", _override_url)

target_metadata = Base.metadata


def _ensure_sqlite_dir(url: str) -> None:
    """Create the parent directory for a SQLite file URL if it doesn't exist."""

    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return
    db_path = Path(url[len(prefix) :])
    db_path.parent.mkdir(parents=True, exist_ok=True)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a live DBAPI)."""

    url = config.get_main_option("sqlalchemy.url")
    assert url is not None, "sqlalchemy.url must be set in alembic.ini"
    _ensure_sqlite_dir(url)
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a live database connection."""

    url = config.get_main_option("sqlalchemy.url")
    assert url is not None, "sqlalchemy.url must be set in alembic.ini"
    _ensure_sqlite_dir(url)

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
