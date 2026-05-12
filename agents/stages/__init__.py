"""LangGraph node implementations for the five Aura pipeline stages.

* :mod:`agents.stages.research` — Tavily web research (degradable per FR-021).
* :mod:`agents.stages.retrieval` — per-brand Chroma similarity search.
* :mod:`agents.stages.copy` — OpenAI-backed copywriter with platform budgets.
* :mod:`agents.stages.image` — Hugging Face SDXL-Turbo image generation.
* :mod:`agents.stages.critic` — OpenAI-backed structured-output critic.

Each stage exposes a single ``run_*`` function whose return tuple includes
the stage's typed Pydantic output plus a ``list[ModelCall]`` describing the
external calls made — the runner persists those into the ``stage_traces``
``model_calls_json`` column.
"""

from __future__ import annotations


class StageError(Exception):
    """Raised by hard-fail stages when an external dependency fails or returns
    malformed output. The runner catches this, records the reason, and
    transitions the run to ``status='failed'``."""


__all__ = ["StageError"]
