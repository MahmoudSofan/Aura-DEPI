"""Pure mapping from ``(stage, status)`` to a [0, 1] progress fraction.

Used by ``GET /api/v1/campaigns/{run_id}`` when assembling the ``Run.progress``
field. Per FR-017 the value is *real* (reflects which stage the orchestrator
is on), not a synthetic timer.
"""

from __future__ import annotations

from typing import Literal

RunStatus = Literal["queued", "running", "done", "failed"]
StageName = Literal["research", "retrieval", "copy", "image", "critic"]

_STAGE_PROGRESS: dict[StageName, float] = {
    "research": 0.10,
    "retrieval": 0.20,
    "copy": 0.50,
    "image": 0.75,
    "critic": 0.90,
}


def progress_for(
    stage: StageName | None,
    status: RunStatus,
    attempt_count: int,
    retry_cap: int,
) -> float:
    """Map run state to a [0, 1] progress value.

    * ``queued`` → 0.0
    * ``running`` → progress of the current stage (or 0.0 if no stage yet)
    * ``done`` → 1.0
    * ``failed`` → progress of the stage at which the failure occurred
      (or 0.0 if the run failed before starting any stage, e.g. via
      ``interrupted_by_restart`` on a queued run)

    ``attempt_count`` and ``retry_cap`` are accepted for forward compat with
    a future per-attempt progress refinement; they are unused in v1.
    """

    del attempt_count, retry_cap  # reserved for future use

    if status == "queued":
        return 0.0
    if status == "done":
        return 1.0
    if status == "failed":
        if stage is None:
            return 0.0
        return _STAGE_PROGRESS.get(stage, 0.0)
    # running
    if stage is None:
        return 0.0
    return _STAGE_PROGRESS.get(stage, 0.0)


__all__ = ["progress_for"]
