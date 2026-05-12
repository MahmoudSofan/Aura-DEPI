# Architecture

This page describes how Aura is put together: the request flow from the
Streamlit UI through the FastAPI backend into the LangGraph pipeline, the
three storage layers that back it, and the lifecycle semantics that make
the system survivable across restarts and bounded under load.

## System overview

```text
                    ┌───────────────────────────────┐
                    │     Streamlit  (port 8501)    │
                    │   frontend/streamlit_app.py   │
                    └─────────────┬─────────────────┘
                                  │  httpx — /api/v1/*
                                  ▼
┌────────────────────────────────────────────────────────────────────────┐
│                       FastAPI app (port 8000)                          │
│                          backend/main.py                               │
│                                                                        │
│   ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌─────────┐  │
│   │ healthz  │  │  brands  │  │documents │  │campaigns │  │artifacts│  │
│   └──────────┘  └─────┬────┘  └─────┬────┘  └─────┬────┘  └────┬────┘  │
│                       │             │             │            │       │
│         ┌─────────────┴─────────────┘             │            │       │
│         ▼                                         ▼            │       │
│  ┌─────────────────────────────────┐    ┌────────────────────┐ │       │
│  │ rag.ingest                      │    │ orchestrator       │ │       │
│  │   parse → chunk → embed →       │    │   CampaignRunner   │ │       │
│  │   upsert(Chroma)                │    │   semaphore + queue│ │       │
│  └─────────────────────────────────┘    └─────────┬──────────┘ │       │
│                                                   │            │       │
│                                                   ▼            │       │
│                                          ┌─────────────────┐   │       │
│                                          │ agents.graph    │   │       │
│                                          │  (LangGraph)    │   │       │
│                                          └─────────────────┘   │       │
└──────────────────────────────────┬────────────────────┬────────┴───────┘
                                   │                    │
              ┌────────────────────┘                    │
              ▼                                         ▼
   ┌──────────────────┐                       ┌──────────────────┐
   │ ChromaDB :8001   │                       │ SQLite           │
   │  brand_{id} cols │                       │ backend/data/    │
   │                  │                       │   aura.db        │
   └──────────────────┘                       └──────────────────┘
                                                       │
                                                       ▼
                                              ┌──────────────────┐
                                              │ Filesystem       │
                                              │ backend/data/    │
                                              │   uploads/{bid}/ │
                                              │   artifacts/{bid}│
                                              └──────────────────┘
                              ┌──────────────────┐
                              │ MLflow :5000     │
                              │  one run / run   │
                              └──────────────────┘
```

## Components

### FastAPI app — `backend/main.py`

Constructs the app, wires versioned routers under `/api/v1`, registers
three exception handlers (HTTPException, RequestValidationError, generic),
and defines the lifespan:

- on startup — `run_interrupt_sweep()` marks any run still in
  `queued`/`running` from a prior process as `failed` with
  `reason='interrupted_by_restart'` (FR-025), then constructs and starts
  the `CampaignRunner` (the async queue consumer).
- on shutdown — `runner.stop()` cancels the consumer task.

Legacy unversioned routes (`POST /api/campaigns/generate`,
`GET /api/campaigns/{run_id}/status`) emit `308` redirects into `/api/v1`;
`POST /api/documents/upload` returns `410 Gone` because the new route is
brand-scoped and a redirect would lose information.

### Routers — `backend/api/`

| File | Routes |
|------|--------|
| [healthz.py](../backend/api/healthz.py) | `GET /api/v1/healthz` — liveness + dependency probe. |
| [brands.py](../backend/api/brands.py) | `POST /brands`, `GET /brands`, `GET /brands/{id}`, `DELETE /brands/{id}` (cascade). |
| [documents.py](../backend/api/documents.py) | `POST /brands/{id}/documents`, `GET /brands/{id}/documents`. |
| [campaigns.py](../backend/api/campaigns.py) | `POST /campaigns`, `GET /campaigns`, `GET /campaigns/{run_id}`. |
| [artifacts.py](../backend/api/artifacts.py) | `GET /artifacts/{brand_id}/{filename}` — PNG streaming with brand-existence + traversal validation. |

The submit-campaign handler ([backend/api/campaigns.py:131](../backend/api/campaigns.py#L131)) does three things, in order:

1. Validates the brand exists (404 otherwise).
2. INSERTs a `runs` row with `status='queued'` and commits.
3. Calls `runner.enqueue(run_id)` (non-blocking).

That keeps the SC-002 submission budget (<1s at p95) tight regardless of
how busy the pipeline is.

### Orchestrator — `backend/orchestrator/`

[CampaignRunner](../backend/orchestrator/runner.py) is the heart of the
control plane. It owns:

- an `asyncio.Semaphore(N)` (default `N=5`, env `AURA_CONCURRENCY_CAP`) that bounds the number of *executing* runs (FR-010).
- an `asyncio.Queue[str]` of pending `run_id`s.
- a consumer task that pops queued ids and dispatches each through the semaphore.

For each dispatched run, `_run_one` resolves the run record, marks it
`running` with `current_stage='research'`, builds a Chroma client, then
invokes `agents.graph.run(...)` with an `on_stage_event` callback that
persists each stage trace entry as it fires. On success it picks the
highest-scoring attempt, promotes its image to the canonical
`{run_id}.png` path, deletes the losing attempts, writes the
`campaign_outputs` row, transitions the run to `done`, batches the run to
MLflow, then prunes oldest terminal runs for that brand beyond the
retention cap (FR-022).

Two other small pieces live in this package:

- [`interrupt_sweeper.run_interrupt_sweep()`](../backend/orchestrator/interrupt_sweeper.py) — the FR-025 startup sweep.
- [`progress.progress_for(...)`](../backend/orchestrator/progress.py) — maps `(status, current_stage, attempt_count, retry_cap)` to a fraction in `[0, 1]` for the status payload.

### Pipeline — `agents/graph.py` + `agents/stages/`

LangGraph state machine, five nodes. Topology:

```text
START ──┬─► research ──┐
        │              ├─► copy ─► image ─► critic ──┬─► END   (pass / cap)
        └─► retrieval ─┘     ▲                       │
                             └───────────────────────┘ (retry with feedback)
```

`research` and `retrieval` execute in parallel on attempt 1 (FR-009);
later attempts reuse their outputs. `copy`, `image`, and `critic` run
sequentially per attempt. After the critic returns, a conditional edge
routes back to `copy` if `will_retry` is true (verdict failed AND
`attempt < retry_cap + 1`), otherwise to `END`. The graph collects every
attempt's `(ad_copy, image, critic)` triple; the runner picks the highest-
scoring one as the winner (FR-016). Full per-stage details are in
[agents.md](agents.md).

### Persistence — `backend/persistence/`

SQLAlchemy 2.x sync sessions over SQLite (`backend/data/aura.db`),
Alembic-managed schema. Three repositories (`BrandRepository`,
`DocumentRepository`, `RunRepository` + `StageTraceRepository`) own all
SQL. Stage-level operations run inside `asyncio.to_thread` from the runner
so the event loop is never blocked on the SQL driver. ON DELETE CASCADE
foreign keys make the brand-delete cascade (FR-024) atomic in one
transaction; the orchestrator then deletes the per-brand ChromaDB
collection and the `uploads/{brand_id}/` and `artifacts/{brand_id}/`
directories after the SQL commit succeeds.

### RAG ingest — `rag/`

[`rag.ingest.ingest_document`](../rag/ingest.py) runs the full pipeline:
hash → dedup check → size check → save raw bytes → parse (PDF / DOCX /
TXT / MD) → chunk (tiktoken ~500 tokens, 50 overlap) → embed (sentence-
transformers `all-MiniLM-L6-v2`) → upsert into the per-brand Chroma
collection `brand_{brand_id}`. The retrieval stage at run time queries
only that collection — there is no API path that returns chunks from a
different brand's collection, which is how FR-005 brand isolation is
enforced *structurally* rather than as a "don't forget the where-clause"
discipline.

Full detail in [rag.md](rag.md).

### Frontend — `frontend/streamlit_app.py`

Streamlit single-file app with sidebar tabs (Brands, Documents, Campaign
request, Results, Runs). Talks to the API via `httpx`; base URL comes from
`AURA_API_BASE` (default `http://localhost:8000`). The Results view polls
`GET /api/v1/campaigns/{run_id}` every 2 seconds and renders the per-stage
trace as it accumulates. The campaign image is loaded by URL
(`/api/v1/artifacts/...`) — image bytes are never inlined in JSON
responses (FR-023).

## Three storage layers

| Layer | Backing | Holds |
|-------|---------|-------|
| **Relational** | SQLite (`backend/data/aura.db`) | brands, documents, runs, stage_traces, campaign_outputs |
| **Vector** | ChromaDB compose service (host port 8001) | one collection per brand `brand_{brand_id}` with embedded chunks |
| **Files** | Filesystem under `backend/data/` | raw uploaded documents and generated PNG images |

The split is deliberate: ChromaDB has no relational/cascade semantics,
SQLite is a poor store for multi-MB BLOBs, and inlining image bytes in
JSON would violate FR-023. See [`research.md` §"Complexity Tracking"](../specs/001-aura-marketing-platform/plan.md#complexity-tracking) for the
trade-off discussion.

## Lifecycle semantics

### Run state machine

```text
   ┌──────┐  enqueue   ┌────────┐  graph done    ┌──────┐
   │queued├───────────►│running ├───────────────►│ done │
   └──┬───┘            └───┬────┘                └──────┘
      │                    │ stage hard-fail ─►  ┌──────┐
      │                    └────────────────────►│failed│
      │ FR-025 startup sweep ─────────────────►  └──────┘
      │ brand delete (FR-024) ────────────────►
```

`done` and `failed` are terminal. The runner only transitions to `done`
once the `campaign_outputs` row has been written; a `failed` run never
gets one (FR-018).

### Concurrency and queueing (FR-010)

The runner accepts unlimited queued submissions (`asyncio.Queue` has no
fixed bound) — submission is never rejected on the basis of "too busy".
Up to `AURA_CONCURRENCY_CAP` runs execute in parallel; additional ones
wait in `queued` state until a semaphore slot frees. The submission API
returns the `run_id` immediately after the SQL insert + enqueue — that's
why SC-002 (<1s p95) holds independent of pipeline load.

### Restart survivability (FR-020 + FR-025)

Every state transition is committed to SQLite before any downstream action
happens. On startup, `run_interrupt_sweep()` finds rows still in
non-terminal state and transitions each to `failed` with
`failed_reason='interrupted_by_restart'`; their stage traces up to the
interruption remain queryable (FR-019 / SC-006). There is no checkpoint /
resume — operators recover by resubmitting (deliberate v1 scope; see
[`research.md §3`](../specs/001-aura-marketing-platform/research.md)).

### Retention (FR-022)

After each terminal transition the runner prunes the oldest terminal runs
for that brand beyond `AURA_RUN_RETENTION_PER_BRAND` (default 100), and
the prune deletes the matching artifact PNG files in the same operation.
In-flight runs are never pruned.

### Brand delete cascade (FR-024)

A single SQL transaction deletes the brand row and (via `ON DELETE
CASCADE`) its documents, stage_traces, runs, and campaign_outputs. After
commit, the orchestrator removes the brand's ChromaDB collection and the
two on-disk directories (`uploads/{brand_id}/`, `artifacts/{brand_id}/`).
In-flight runs for the brand are cancelled before the cascade completes.

## External dependencies and failure classes (FR-021)

| Stage | Provider | Failure class | If unavailable |
|-------|----------|---------------|----------------|
| research | Tavily (`tavily-python`) | **degradable** | run continues with empty `ResearchOutput`, trace marked `degraded`, critic informed |
| retrieval | local sentence-transformers + ChromaDB | **hard-fail** | run terminates `failed` with stage='retrieval' |
| copy | OpenRouter (OpenAI-compatible chat completions) | **hard-fail** | run terminates `failed` with stage='copy' |
| image | OpenRouter (chat completions w/ `modalities=["image","text"]`) | **hard-fail** | run terminates `failed` with stage='image' |
| critic | OpenRouter | **hard-fail** | run terminates `failed` with stage='critic' |

The `ModelCall.provider` literal (`openrouter | huggingface | tavily`) in
[`agents/schemas.py`](../agents/schemas.py) is the canonical list of
external providers Aura calls. Note: `huggingface` here refers to the
local sentence-transformers embedding (in-process, not a network call) —
the LLM and image model both ride on OpenRouter chat completions. This
differs from [`research.md §4-5`](../specs/001-aura-marketing-platform/research.md)
which proposed direct OpenAI + HF Inference API; the deployment converged
on OpenRouter for both text and image since OpenRouter supports
`modalities=["image","text"]` for image generation and gives a single
auth/billing surface.
