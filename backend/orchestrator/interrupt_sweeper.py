"""FR-025 startup sweeper.

On API process boot, mark every ``runs`` row whose ``status`` is non-terminal
as ``failed`` with ``failed_reason='interrupted_by_restart'``. The
per-stage trace rows are preserved so operators can audit what work the
prior process had completed before the bounce.

Wired into :mod:`backend.main` via ``app.add_event_handler("startup",
run_interrupt_sweep)``.
"""

from __future__ import annotations

import logging

from backend.persistence.repository import RunRepository
from backend.persistence.session import session_scope

logger = logging.getLogger("aura.orchestrator.sweeper")


def run_interrupt_sweep() -> list[str]:
    """Fail any non-terminal run found at boot. Returns the swept run_ids."""

    with session_scope() as session:
        swept = RunRepository.sweep_non_terminal_to_failed(session)

    if swept:
        logger.warning(
            "boot: marked %d non-terminal run(s) as failed (interrupted_by_restart): %s",
            len(swept),
            swept,
        )
    else:
        logger.info("boot: no non-terminal runs to sweep")
    return swept


__all__ = ["run_interrupt_sweep"]
