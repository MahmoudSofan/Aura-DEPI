"""Orchestrator: wires the API to the LangGraph pipeline.

Exposed surface:

* :func:`run_interrupt_sweep` — startup hook that fails any non-terminal
  runs left over from a prior process (FR-025).
* :func:`progress_for` — pure mapping from ``(stage, status)`` to a [0, 1]
  progress fraction (FR-017).

The ``CampaignRunner`` lands in T032; this package just contains the
restart sweeper, the progress mapping, and the runner once it exists.
"""

from __future__ import annotations

from backend.orchestrator.interrupt_sweeper import run_interrupt_sweep
from backend.orchestrator.progress import progress_for

__all__ = ["progress_for", "run_interrupt_sweep"]
