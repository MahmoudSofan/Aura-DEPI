"""Shared pytest fixtures for the Aura backend test suite.

Goals (per `research.md §16`):

* Determinism — no live API calls in default ``pytest`` runs.
* Offline runnability — embedded ChromaDB, in-process SQLite under a temp dir.
* Real persistence layer — SQLite + Chroma run for real (in embedded mode)
  so the cascade and restart-sweeper tests cover their actual code paths.

Stub fixtures (``stub_openai``, ``stub_image``, ``stub_tavily``) install
monkey-patches on the per-stage client factories defined in T026–T030 so
tests can push canned responses and assert on the call args the stages
produced.
"""

from __future__ import annotations

import contextlib
from collections.abc import Generator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture
def tmp_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Per-test ``AURA_DATA_DIR`` rooted under pytest's tmp_path."""

    data_dir = tmp_path / "aura_data"
    (data_dir / "uploads").mkdir(parents=True, exist_ok=True)
    (data_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AURA_DATA_DIR", str(data_dir))
    return data_dir


@pytest.fixture
def db_url(tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """SQLAlchemy URL pointing at the per-test SQLite file."""

    url = f"sqlite:///{(tmp_data_dir / 'aura.db').as_posix()}"
    monkeypatch.setenv("AURA_DATABASE_URL", url)
    return url


@pytest.fixture
def engine(db_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    """Create a fresh engine bound to the per-test DB and apply migrations."""

    from alembic import command
    from alembic.config import Config
    from backend.persistence import session as session_module
    from backend.persistence.session import create_engine_for_url

    test_engine = create_engine_for_url(db_url)

    alembic_cfg = Config(str(Path(__file__).resolve().parents[1] / "persistence" / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(alembic_cfg, "head")

    test_session_local = sessionmaker(
        bind=test_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        future=True,
    )
    monkeypatch.setattr(session_module, "engine", test_engine)
    monkeypatch.setattr(session_module, "SessionLocal", test_session_local)

    try:
        yield test_engine
    finally:
        test_engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """Yield a session bound to the per-test engine (auto-rollback on exit)."""

    from backend.persistence import session as session_module

    test_session = session_module.SessionLocal()
    try:
        yield test_session
    finally:
        test_session.rollback()
        test_session.close()


def _make_isolated_chroma_client() -> Any:
    """Build a fresh chromadb client with state reset.

    chromadb's ``EphemeralClient`` shares process-wide SQLite state across
    instances, so naive ``EphemeralClient()`` calls in different tests see
    each other's data. Building a `Client(Settings(allow_reset=True))` and
    calling ``reset()`` clears that shared state.
    """

    import chromadb
    from chromadb.config import Settings

    client = chromadb.Client(Settings(is_persistent=False, allow_reset=True))
    client.reset()
    return client


@pytest.fixture
def chroma_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Embedded (in-process) ChromaDB client + redirect runner factory.

    The client is reset before yield so state from earlier tests doesn't
    leak in.
    """

    client = _make_isolated_chroma_client()
    monkeypatch.setattr(
        "backend.orchestrator.runner._make_default_chroma_client",
        lambda: client,
    )
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            client.reset()


@pytest.fixture
def stub_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the sentence-transformers embedder with a deterministic hash-based stub.

    Both ingest (``rag.embeddings.embed_batch``) and retrieval (which calls
    the same wrapper) hit this; the resulting vectors live in the same
    space so similarity search still ranks chunks containing the query
    tokens above unrelated chunks.
    """

    import hashlib

    def _vec(text: str, dim: int = 384) -> list[float]:
        # Token-level hashing: each lowercase alphanumeric token contributes
        # a small bump in a deterministic dimension, so chunks/queries that
        # share tokens get high cosine similarity.
        import re

        v = [0.0] * dim
        for tok in re.findall(r"[a-zA-Z0-9]+", text.lower()):
            h = int(hashlib.sha1(tok.encode("utf-8")).hexdigest(), 16)
            v[h % dim] += 1.0
        # L2-normalise so the dot product behaves like cosine similarity.
        norm = sum(x * x for x in v) ** 0.5
        if norm == 0:
            v[0] = 1.0
            return v
        return [x / norm for x in v]

    def fake_embed_batch(texts: list[str]) -> list[list[float]]:
        return [_vec(t) for t in texts]

    monkeypatch.setattr("rag.embeddings.embed_batch", fake_embed_batch)
    # Retrieval imports `embed_batch` lazily inside `_embed_query`, so the
    # patch on `rag.embeddings.embed_batch` is sufficient for both call
    # sites.


# ---------------------------------------------------------------------------
# External-client stub recorders.
# ---------------------------------------------------------------------------


@dataclass
class StubRecorder:
    """Minimal call recorder + canned-response queue."""

    name: str
    responses: list[Any] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def push(self, response: Any) -> None:
        self.responses.append(response)

    def push_exception(self, exc: BaseException) -> None:
        self.responses.append(exc)

    def record(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError(
                f"{self.name} stub has no canned response queued for call: {kwargs!r}"
            )
        next_response = self.responses.pop(0)
        if isinstance(next_response, BaseException):
            raise next_response
        return next_response


def _make_openai_chat_response(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=42, completion_tokens=24),
    )


@pytest.fixture
def stub_openai(monkeypatch: pytest.MonkeyPatch) -> StubRecorder:
    """Patches the OpenRouter LLM client used by both copy and critic stages.

    Named ``stub_openai`` for continuity with prior-phase tests; under the
    hood it patches ``_make_llm_client`` (which now points at OpenRouter's
    OpenAI-compatible endpoint).
    """

    rec = StubRecorder(name="openrouter-llm")

    def _create(**kwargs: Any) -> SimpleNamespace:
        result = rec.record(**kwargs)
        if isinstance(result, str):
            return _make_openai_chat_response(result)
        if not isinstance(result, SimpleNamespace):
            raise TypeError(
                f"stub_openai responses must be str or SimpleNamespace; got {type(result)}"
            )
        return result

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=_create)),
    )
    factory = lambda: fake_client  # noqa: E731
    monkeypatch.setattr("agents.stages.copy._make_llm_client", factory)
    # critic imports `_make_llm_client` by name from copy at module load
    # time, so the imported binding lives on the critic module too — patch
    # both so either call site sees the stub.
    monkeypatch.setattr("agents.stages.critic._make_llm_client", factory)
    return rec


def _encode_pil_to_b64_png(img: Any) -> str:
    """Encode a PIL.Image (or any object with ``save``) as base64 PNG."""

    import base64
    import io

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


@pytest.fixture
def stub_image(monkeypatch: pytest.MonkeyPatch) -> StubRecorder:
    """Patches ``image._make_image_client`` (chat-completions with image modality).

    OpenRouter routes image generation through ``/chat/completions`` with
    ``modalities=["image","text"]``; the response carries the PNG as a
    ``data:image/png;base64,...`` URL on ``message.images[0].image_url.url``.
    Pushed responses can be:
      - PIL.Image — auto-encoded into the chat-image response shape.
      - str — treated as a base64 PNG payload, wrapped into a data URL.
      - SimpleNamespace — used as-is (escape hatch for custom shapes).
    """

    rec = StubRecorder(name="openrouter-image")

    def _create(**kwargs: Any) -> SimpleNamespace:
        result = rec.record(**kwargs)
        if isinstance(result, str):
            b64 = result
        elif hasattr(result, "save") and callable(result.save):
            b64 = _encode_pil_to_b64_png(result)
        elif isinstance(result, SimpleNamespace):
            return result
        else:
            raise TypeError(
                f"stub_image responses must be PIL.Image, str (b64), or SimpleNamespace; "
                f"got {type(result)}"
            )
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

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))
    monkeypatch.setattr("agents.stages.image._make_image_client", lambda: fake_client)
    return rec


@pytest.fixture
def stub_tavily(monkeypatch: pytest.MonkeyPatch) -> StubRecorder:
    """Patches ``research._make_tavily_client``."""

    rec = StubRecorder(name="tavily")

    def _search(**kwargs: Any) -> Any:
        return rec.record(**kwargs)

    fake_client = SimpleNamespace(search=_search)
    monkeypatch.setattr("agents.stages.research._make_tavily_client", lambda: fake_client)
    monkeypatch.setenv("TAVILY_API_KEY", "test-stub-key")
    return rec


# ---------------------------------------------------------------------------
# Test data builders.
# ---------------------------------------------------------------------------


def make_pil_image(width: int = 64, height: int = 64) -> Any:
    """Return a small PIL.Image so ``image.save(...)`` succeeds in stub paths."""

    from PIL import Image

    return Image.new("RGB", (width, height), color=(128, 64, 200))


def make_critic_response(
    *,
    overall: float = 0.85,
    breakdown: dict[str, float] | None = None,
    feedback: str = "Solid work.",
) -> str:
    import json

    breakdown = breakdown or {
        "relevance": 0.9,
        "brand_fit": 0.85,
        "clarity": 0.82,
        "factual_alignment": 0.83,
    }
    return json.dumps(
        {
            "overall": overall,
            "breakdown": breakdown,
            "feedback": feedback,
            "passed": overall >= 0.7,
        }
    )


def make_copy_response(
    *,
    headline: str = "Headline.",
    primary_text: str = "Primary text body.",
    cta: str = "Shop now",
    platform: str = "instagram",
) -> str:
    import json

    return json.dumps(
        {
            "headline": headline,
            "primary_text": primary_text,
            "cta": cta,
            "platform": platform,
        }
    )


# ---------------------------------------------------------------------------
# FastAPI TestClient.
# ---------------------------------------------------------------------------


@pytest.fixture
def shared_chroma(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """A single-instance Chroma client shared by every consumer in the test.

    Using one shared instance means uploads via the documents API and
    queries via the retrieval stage talk to the same in-memory store. The
    client is reset before yield so state from earlier tests doesn't leak.
    """

    client = _make_isolated_chroma_client()
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            client.reset()


@pytest.fixture
def api_client(
    engine: Engine,
    shared_chroma: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[TestClient, None, None]:
    """FastAPI TestClient with the per-test DB and stub clients wired in.

    The runner's chroma factory returns the *same* :func:`shared_chroma`
    instance every call, which is what tests expect when they upload via
    the API and then read back via retrieval.
    """

    monkeypatch.delenv("CHROMA_HOST", raising=False)
    monkeypatch.delenv("CHROMA_PORT", raising=False)

    monkeypatch.setattr(
        "backend.orchestrator.runner._make_default_chroma_client",
        lambda: shared_chroma,
    )

    from backend.main import app
    from backend.persistence.session import get_session

    def _override_get_session() -> Generator[Session, None, None]:
        from backend.persistence import session as session_module

        s = session_module.SessionLocal()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[get_session] = _override_get_session
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_session, None)


# ---------------------------------------------------------------------------
# Test-only env isolation + helpers.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_aura_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure tests don't accidentally pick up the developer's API keys."""

    for var in (
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "HF_TOKEN",
        "TAVILY_API_KEY",
        "AURA_API_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AURA_LLM_MODEL", "openai/gpt-4o-mini")
    monkeypatch.setenv("AURA_IMAGE_MODEL", "google/gemini-2.5-flash-image-preview")
    monkeypatch.setenv("AURA_CRITIC_THRESHOLD", "0.7")
    monkeypatch.setenv("AURA_RETRY_CAP", "2")
    monkeypatch.setenv("AURA_CONCURRENCY_CAP", "5")
    monkeypatch.setenv("AURA_RUN_RETENTION_PER_BRAND", "100")
    # Dummy key so the OpenRouter-backed copy/critic/image stages don't bail
    # out before the stub patches take effect. Tests that exercise the
    # missing-key path delete the var explicitly.
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-stub-openrouter-key")


@pytest.fixture(autouse=True)
def _noop_mlflow(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip MLflow logging in tests so we don't retry-storm against
    a non-existent tracking server (MLflow downtime must never break a run
    per the eval/stage_tracking.py contract; this just enforces it for tests)."""

    monkeypatch.setattr(
        "backend.orchestrator.runner.log_aura_run_to_mlflow",
        lambda *args, **kwargs: None,
    )


@pytest.fixture
def seeded_brand_id(session: Session) -> str:
    """A deterministic brand id persisted in the test DB."""

    from backend.persistence.repository import BrandRepository

    brand_id = "01HXTESTBRAND0000000000000"
    if BrandRepository.get(session, brand_id) is None:
        BrandRepository.create(session, brand_id=brand_id, display_name="Test Brand")
    session.commit()
    return brand_id


@pytest.fixture
def seeded_brand_id_via_client(api_client: TestClient) -> str:
    """Like seeded_brand_id but using the api_client's DB connection."""

    from backend.persistence import session as session_module
    from backend.persistence.repository import BrandRepository

    brand_id = "01HXTESTBRAND0000000000000"
    with session_module.SessionLocal() as s:
        if BrandRepository.get(s, brand_id) is None:
            BrandRepository.create(s, brand_id=brand_id, display_name="Test Brand")
        s.commit()
    return brand_id
