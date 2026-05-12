# Testing

How Aura is tested, where each kind of test lives, and how to opt into
the live-mode smoke against real OpenRouter / Tavily.

## Test stack

- **pytest** ≥7.0 with `--import-mode=importlib` (so leaf modules like `agents/tests/test_critic.py` and `backend/tests/test_campaigns_contract.py` don't collide).
- **httpx** + FastAPI `TestClient` for API contract tests.
- **SQLite** real (not mocked) in integration tests — temp file per test session.
- **ChromaDB** real, in-process `EphemeralClient` — no compose dependency at test time.
- **OpenRouter / Tavily / image clients** stubbed by default — see "How stubs are wired" below.

## Layout

```text
agents/tests/        # per-stage unit tests
  test_copy_platform_budgets.py
  test_critic.py
  test_critic_pass_on_first.py
  test_critic_retry_cap_exhaustion.py
  test_critic_retry_pass_on_second.py
  test_image_failure.py
  test_research_degradation.py

backend/tests/       # API contract + orchestrator integration tests
  conftest.py        # shared fixtures (api_client, stubs, engine, ...)
  test_artifacts_contract.py
  test_brand_delete_cancels_inflight.py
  test_brand_delete_cascade.py
  test_brand_isolation.py
  test_brands_contract.py
  test_campaigns_contract.py
  test_campaigns_integration.py
  test_completed_run_survives_restart.py
  test_concurrency.py
  test_documents_contract.py
  test_foundational.py
  test_grounded_copy_integration.py
  test_quickstart_walkthrough.py
  test_restart_sweeper.py
  test_run_retention.py
  integration/

eval/tests/
  test_stage_tracking.py   # MLflow logging shape

rag/tests/
  test_chunking.py
  test_ingest.py
  test_parsers.py
```

`conftest.py` at the repo root registers `backend.tests.conftest` as a
plugin so the same fixtures (`api_client`, `stub_openai`, `stub_hf`,
`stub_tavily`, `engine`) are available everywhere — no per-subtree
duplication.

## Run it

```powershell
# Full suite (deterministic, offline, no API spend)
pytest

# One file
pytest backend/tests/test_campaigns_contract.py

# One test by name
pytest -k test_brand_isolation

# With verbose output
pytest -v backend/tests/test_quickstart_walkthrough.py
```

`pytest` config lives in [pyproject.toml](../pyproject.toml) under
`[tool.pytest.ini_options]`. `testpaths` is `backend agents rag eval`,
`--strict-markers --strict-config` is on, and the `live` marker is
declared so live tests are explicit.

## How stubs are wired

The five external dependencies (OpenRouter LLM client, OpenRouter image
client, Tavily client, ChromaDB, MLflow) are all reachable through
**module-level factory functions** that tests monkey-patch:

| Module | Factory | Stubbed by |
|--------|---------|-----------|
| `agents.stages.copy` | `_make_llm_client()` | `stub_openai` fixture sets canned chat-completion responses |
| `agents.stages.critic` | shares `_make_llm_client` with copy | `stub_openai.set_critic_response(json_str)` |
| `agents.stages.image` | `_make_image_client()` | `stub_hf` fixture (named for historical reasons; serves PNG bytes) |
| `agents.stages.research` | `_make_tavily_client()` | `stub_tavily` fixture |
| `backend.orchestrator.runner._make_default_chroma_client` | module-level | swapped for `chromadb.EphemeralClient()` in tests |
| MLflow tracking URI | `MLFLOW_TRACKING_URI` env | tests set it to a temp dir; logger swallows failures anyway |

The harness for the smoke benchmarks
([`eval/benchmarks/_harness.py`](../eval/benchmarks/_harness.py))
reuses the same fixtures via direct monkey-patching so a benchmark
script can run standalone (no pytest required).

## Live tests

The `live` marker gates tests that hit real external services. They're
**excluded** from the default run.

```powershell
$env:AURA_RUN_LIVE_TESTS = "1"
pytest -m live
```

Live tests verify the actual integration — that the OpenRouter chat
completion really returns parsable JSON for the critic, that image
generation really produces PNG bytes — but they spend OpenRouter credits
and are non-deterministic by their nature. Run them sparingly:

- before merging a change that touches `agents/stages/*.py`;
- after upgrading OpenAI / OpenRouter / Tavily SDK versions;
- when investigating a quality-class issue (low critic scores, repeated retries) that doesn't reproduce on stubs.

## What each layer of tests guards

### Stage unit tests — `agents/tests/`

- Each stage's happy path with stubbed clients.
- Critic retry behaviour: pass on first attempt, pass on second, retry-cap exhaustion (FR-015, FR-016).
- Research degradation (FR-021): missing key, Tavily timeout.
- Image hard-fail (FR-021).
- Copy platform-length budgets (FR-011 — per-platform prompt assertions).

### RAG tests — `rag/tests/`

- Parser format coverage (PDF, DOCX, TXT, MD); rejected formats.
- Chunking: ~500-token chunks, 50-token overlap, provenance fields.
- Ingest pipeline: dedup (FR-004), size cap, parse-rejected row retention.

### Backend contract tests — `backend/tests/test_*_contract.py`

- Each `/api/v1/*` endpoint's request/response shape against
  [`contracts/openapi.yaml`](../specs/001-aura-marketing-platform/contracts/openapi.yaml).
- Status codes for 404 / 409 / 413 / 415 / 422 / 503.
- Legacy 308 redirects and 410 for the documents-upload route.

### Backend integration tests — `backend/tests/test_*_integration.py`, `test_quickstart_walkthrough.py`, ...

- End-to-end happy path via the orchestrator (mirrors quickstart §1–§6).
- Brand isolation (FR-005) — retrieval for Brand A never returns Brand B chunks.
- Brand delete cascade (FR-024) — SQL + Chroma + filesystem all cleaned.
- Brand delete cancels in-flight runs (FR-024).
- Run retention cap pruning (FR-022) — oldest beyond cap removed, artifacts deleted.
- Restart sweeper (FR-025) — `queued`/`running` rows flipped to `failed: interrupted_by_restart` on boot.
- Completed run survives restart (FR-020 / SC-006).
- Concurrency cap (FR-010 / SC-008) — N submissions, M execute, M+1..N queued.

### MLflow logging — `eval/tests/test_stage_tracking.py`

- The MLflow run payload shape (params, metrics, artifacts).
- Logger failure-mode: MLflow downtime swallowed, never propagated.

## Smoke benchmarks vs. tests

Tests live under `*/tests/` and run on every `pytest`. Benchmarks live
under `eval/benchmarks/` and are **not** collected by default — they're
standalone scripts:

```powershell
python -m eval.benchmarks.e2e_latency_smoke
python -m eval.benchmarks.concurrency_smoke
python -m eval.benchmarks.repeatability_smoke
python -m eval.benchmarks.brand_grounding_smoke
```

See [evaluation.md](evaluation.md#smoke-benchmarks) for what each one
asserts.

## Pre-merge gate

The local equivalent of CI:

```powershell
ruff check .
ruff format --check .
mypy
pytest
```

All four must pass. The PROJECT_MILESTONES.md M4 plan calls for GitHub
Actions to run this on every push — that wiring is a known gap (see
[operations.md#known-gaps](operations.md#known-gaps)).

## Test conventions

- **Mypy strict** covers `backend`, `agents`, `rag`, `eval` — but tests are exempt from `disallow_untyped_defs` (see `[[tool.mypy.overrides]]` in `pyproject.toml`).
- Test functions use `test_*` naming and live in `test_*.py` files (pytest defaults).
- `@pytest.mark.live` marks tests requiring real external services; gated by `AURA_RUN_LIVE_TESTS=1`.
- Fixtures that mutate global state (env vars, monkey-patched factories) are scoped to the test that needs them and revert on teardown.
