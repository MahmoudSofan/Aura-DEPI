"""Shared rig for the smoke benchmarks.

Each public function in this module composes the bits that the pytest
``conftest.py`` wires up declaratively:

* a temporary ``AURA_DATA_DIR`` (fresh SQLite + uploads/artifacts dirs),
* migrations applied,
* an in-process ChromaDB client redirected into the runner factory,
* monkey-patches that swap the OpenRouter LLM, image, and Tavily clients
  for canned-response stubs,
* a FastAPI ``TestClient`` bound to that stack.

Benchmark scripts call :func:`build_stack` to get back a ready-to-use
``BenchStack`` and then drive it with ``client.post(...)``.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient


@dataclass
class BenchStack:
    """Bundle of resources an Aura benchmark needs to drive the API."""

    client: TestClient
    brand_id: str
    set_copy_response: Callable[[str], None]
    set_critic_response: Callable[[str], None]
    set_image_response: Callable[[bytes], None]
    cleanup: Callable[[], None] = field(default=lambda: None)


def _make_critic_payload(*, overall: float, feedback: str = "ok") -> str:
    return json.dumps(
        {
            "overall": overall,
            "breakdown": {
                "relevance": overall,
                "brand_fit": overall,
                "clarity": overall,
                "factual_alignment": overall,
            },
            "feedback": feedback,
            "passed": overall >= 0.7,
        }
    )


def _make_copy_payload(headline: str, *, primary_text: str = "primary", cta: str = "Shop") -> str:
    return json.dumps(
        {
            "headline": headline,
            "primary_text": primary_text,
            "cta": cta,
            "platform": "instagram",
        }
    )


def _make_png_bytes(width: int = 64, height: int = 64) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(128, 64, 200)).save(buf, "PNG")
    return buf.getvalue()


@contextlib.contextmanager
def build_stack(*, brand_display_name: str = "Bench Brand") -> Iterator[BenchStack]:
    """Spin up a self-contained Aura stack for a benchmark.

    The stack uses a temp data dir, an embedded ChromaDB, and stubbed
    external clients. On context exit everything is torn down.
    """

    import chromadb
    from alembic import command
    from alembic.config import Config
    from chromadb.config import Settings

    tmp_root = Path(tempfile.mkdtemp(prefix="aura-bench-"))
    data_dir = tmp_root / "aura_data"
    (data_dir / "uploads").mkdir(parents=True, exist_ok=True)
    (data_dir / "artifacts").mkdir(parents=True, exist_ok=True)

    db_url = f"sqlite:///{(data_dir / 'aura.db').as_posix()}"

    prev_env: dict[str, str | None] = {}
    env_overrides = {
        "AURA_DATA_DIR": str(data_dir),
        "AURA_DATABASE_URL": db_url,
        "AURA_LLM_MODEL": "openai/gpt-4o-mini",
        "AURA_IMAGE_MODEL": "google/gemini-2.5-flash-image-preview",
        "AURA_CRITIC_THRESHOLD": "0.7",
        "AURA_RETRY_CAP": "2",
        "AURA_CONCURRENCY_CAP": "5",
        "AURA_RUN_RETENTION_PER_BRAND": "100",
        "OPENROUTER_API_KEY": "bench-stub-key",
    }
    for var in (
        "OPENAI_API_KEY",
        "HF_TOKEN",
        "TAVILY_API_KEY",
        "AURA_API_TOKEN",
        "CHROMA_HOST",
        "CHROMA_PORT",
    ):
        prev_env[var] = os.environ.pop(var, None)
    for k, v in env_overrides.items():
        prev_env[k] = os.environ.get(k)
        os.environ[k] = v

    # Migrate the temp DB before importing the app so module-level engine init
    # picks up the right URL.
    alembic_root = Path(__file__).resolve().parents[2] / "backend" / "persistence"
    alembic_cfg = Config(str(alembic_root / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(alembic_cfg, "head")

    # Reload session module so it reads the new env.
    import importlib

    from backend.persistence import session as session_module

    importlib.reload(session_module)

    chroma = chromadb.Client(Settings(is_persistent=False, allow_reset=True))
    chroma.reset()

    # Patches.
    import agents.stages.copy as copy_stage
    import agents.stages.critic as critic_stage
    import agents.stages.image as image_stage
    import agents.stages.research as research_stage
    import backend.orchestrator.runner as runner_mod

    # Mutable response slots — benchmarks update these between submits.
    state: dict[str, Any] = {
        "copy": _make_copy_payload("Default headline"),
        "critic": _make_critic_payload(overall=0.85),
        "image": _make_png_bytes(),
    }

    def _llm_create(**kwargs: Any) -> SimpleNamespace:
        # The critic prompt mentions "critic"; copy prompt says "copywriter".
        text_blobs = " ".join(
            m.get("content", "") for m in kwargs.get("messages", []) if isinstance(m, dict)
        )
        content = state["critic"] if "critic" in text_blobs.lower() else state["copy"]
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=42, completion_tokens=24),
        )

    def _image_create(**kwargs: Any) -> SimpleNamespace:
        import base64

        b64 = base64.b64encode(state["image"]).decode("ascii")
        data_url = f"data:image/png;base64,{b64}"
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        role="assistant",
                        content=None,
                        images=[{"image_url": {"url": data_url}}],
                    )
                )
            ]
        )

    fake_llm = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_llm_create))
    )
    fake_image_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_image_create))
    )

    # critic re-imports `_make_llm_client` from copy at module-load time, so we
    # patch both modules' bound name. Use getattr/setattr to dodge mypy's
    # "private name not exported" check on this private cross-module symbol.
    saved_factories = {
        "copy_llm": getattr(copy_stage, "_make_llm_client"),  # noqa: B009
        "critic_llm": getattr(critic_stage, "_make_llm_client"),  # noqa: B009
        "image_client": getattr(image_stage, "_make_image_client"),  # noqa: B009
        "research_client": getattr(research_stage, "_make_tavily_client"),  # noqa: B009
        "chroma_factory": getattr(runner_mod, "_make_default_chroma_client"),  # noqa: B009
    }

    setattr(copy_stage, "_make_llm_client", lambda: fake_llm)  # noqa: B010
    setattr(critic_stage, "_make_llm_client", lambda: fake_llm)  # noqa: B010
    setattr(image_stage, "_make_image_client", lambda: fake_image_client)  # noqa: B010

    # Tavily stub returns no results — research degrades, the run still completes.
    def _tavily_factory() -> Any:
        raise RuntimeError("tavily disabled in benchmark")

    setattr(research_stage, "_make_tavily_client", _tavily_factory)  # noqa: B010
    setattr(runner_mod, "_make_default_chroma_client", lambda: chroma)  # noqa: B010

    # Build app.
    from backend.main import app

    test_client = TestClient(app)
    test_client.__enter__()

    # Seed brand.
    resp = test_client.post(
        "/api/v1/brands",
        json={"display_name": brand_display_name},
    )
    resp.raise_for_status()
    brand_id = resp.json()["id"]

    def _set_copy(headline: str) -> None:
        state["copy"] = _make_copy_payload(headline)

    def _set_critic(payload: str) -> None:
        state["critic"] = payload

    def _set_image(png_bytes: bytes) -> None:
        state["image"] = png_bytes

    def _cleanup() -> None:
        with contextlib.suppress(Exception):
            test_client.__exit__(None, None, None)
        with contextlib.suppress(Exception):
            chroma.reset()
        setattr(copy_stage, "_make_llm_client", saved_factories["copy_llm"])  # noqa: B010
        setattr(critic_stage, "_make_llm_client", saved_factories["critic_llm"])  # noqa: B010
        setattr(image_stage, "_make_image_client", saved_factories["image_client"])  # noqa: B010
        setattr(research_stage, "_make_tavily_client", saved_factories["research_client"])  # noqa: B010
        setattr(runner_mod, "_make_default_chroma_client", saved_factories["chroma_factory"])  # noqa: B010
        for k, v in prev_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    stack = BenchStack(
        client=test_client,
        brand_id=brand_id,
        set_copy_response=_set_copy,
        set_critic_response=_set_critic,
        set_image_response=_set_image,
        cleanup=_cleanup,
    )
    try:
        yield stack
    finally:
        stack.cleanup()


def submit_brief(
    client: TestClient,
    *,
    brand_id: str,
    brief: str = "Launch summer collection.",
    platform: str = "instagram",
    target_audience: str = "18-24 urban",
) -> str:
    """Submit a single campaign and return its ``run_id``."""

    resp = client.post(
        "/api/v1/campaigns",
        json={
            "brief": brief,
            "platform": platform,
            "brand_id": brand_id,
            "target_audience": target_audience,
        },
    )
    resp.raise_for_status()
    return str(resp.json()["run_id"])


def poll_until_terminal(
    client: TestClient,
    run_id: str,
    *,
    timeout_s: float = 90.0,
    interval_s: float = 0.05,
) -> dict[str, Any]:
    """Poll a run until it reaches ``done`` or ``failed`` and return the payload."""

    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        resp = client.get(f"/api/v1/campaigns/{run_id}")
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        if body["status"] in ("done", "failed"):
            return body
        time.sleep(interval_s)
    raise TimeoutError(f"run {run_id} did not finish in {timeout_s}s")


__all__ = [
    "BenchStack",
    "build_stack",
    "poll_until_terminal",
    "submit_brief",
]
