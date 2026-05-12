# Operations

Runtime configuration, observability, deployment, and the
"what does this knob do" reference for everyone running Aura in
anger.

## Environment variables

All env vars are read at startup (or per-call for stage-level overrides
like `AURA_LLM_MODEL`). The runner reads them via `os.getenv(...)` with
typed defaults — no separate config file in v1.

### External services (required for live runs)

| Var | Used by | Notes |
|-----|---------|-------|
| `OPENROUTER_API_KEY` | `copy`, `image`, `critic` stages | Missing key → first hard-fail stage call returns 401 → run fails with stage-level reason |
| `OPENROUTER_BASE_URL` | OpenRouter client | Default `https://openrouter.ai/api/v1`. Override to point at an OpenAI-compatible mock for testing |
| `TAVILY_API_KEY` | `research` stage | **Degradable** — missing key marks research `degraded` and run continues with empty research output (FR-021) |
| `HF_TOKEN` | (reserved) | Not currently used in deployed config; was relevant under the original `research.md §5` plan to use HF Inference. Safe to leave unset |
| `MLFLOW_TRACKING_URI` | MLflow logger | Default `http://localhost:5000`. MLflow being unreachable does NOT break runs |
| `MLFLOW_EXPERIMENT` | MLflow logger | Default `aura` |
| `CHROMA_HOST` | runner Chroma client | When unset → falls back to in-process `EphemeralClient` |
| `CHROMA_PORT` | runner Chroma client | Default 8000 inside compose, 8001 from host |

### Pipeline behaviour

| Var | Default | Range | What it tunes |
|-----|---------|-------|---------------|
| `AURA_LLM_MODEL` | `openai/gpt-4o-mini` (compose) / `gpt-4o-mini` (default in `agents.stages.critic`) | any OpenRouter chat model id | Both copy and critic stages use this |
| `AURA_IMAGE_MODEL` | `google/gemini-2.5-flash-image-preview` | any OpenRouter image-capable chat model id | Image stage |
| `AURA_CRITIC_THRESHOLD` | `0.7` | `[0.0, 1.0]` | Pass/fail cutoff for the critic's `overall` score |
| `AURA_RETRY_CAP` | `2` | `≥ 0` | Max retries on critic rejection. Total attempts ≤ `retry_cap + 1` |
| `AURA_CONCURRENCY_CAP` | `5` | `≥ 1` | Number of simultaneously **executing** runs. Queue depth is unbounded |
| `AURA_RUN_RETENTION_PER_BRAND` | `100` | `≥ 1` | Oldest terminal runs beyond this per brand are pruned (FR-022) |
| `AURA_DATA_DIR` | `backend/data` | any writable path | Root for SQLite + uploads + artifacts |
| `AURA_API_TOKEN` | unset | any string | When set, API requires `Authorization: Bearer <token>` — single-trusted-operator default is unset |

`retry_cap` and `critic_threshold` are **captured into the run row** at
submit time, so behavior of a specific run is reproducible across later
config changes ([backend/api/campaigns.py:138](../backend/api/campaigns.py#L138)).

## Observability

### Per-run

- **API**: `GET /api/v1/campaigns/{run_id}` — full state with embedded `trace` (every stage entry across attempts, ordered by `started_at`) and, for `done` runs, `output`.
- **MLflow**: one run per Aura run under experiment `aura`, named `aura-{run_id}` — see [evaluation.md](evaluation.md).
- **Filesystem artefacts**: the winning attempt's PNG at `backend/data/artifacts/{brand_id}/{run_id}.png`; raw uploaded documents at `backend/data/uploads/{brand_id}/{document_id}.{ext}`.

### Operational

- **Health probe**: `GET /api/v1/healthz` — `200` with `{status: ok, dependencies: {sqlite, chromadb}}` or `503` with `degraded`.
- **Logs**: stdout, structured Python logging under loggers `aura.api`, `aura.orchestrator.runner`, `aura.stages.*`, `aura.rag.*`. Format: `%(asctime)s %(levelname)s %(name)s: %(message)s`.
- **Run lifecycle log lines**:
  - `campaign queued run_id=... brand_id=...` (submission)
  - `runner: pipeline crashed for run ...` (unhandled in graph)
  - `retention: pruned N run(s) for brand ...` (after each terminal transition)

### What MLflow doesn't see

MLflow logging only fires **after** a run reaches a terminal state. Two
consequences:

- In-flight runs aren't visible in MLflow until they finish — use the API trace for live observability.
- If the API process crashes mid-run, that run never gets an MLflow record. After restart it's marked `failed: interrupted_by_restart` (FR-025) but the MLflow record for it is absent. Use the SQLite trace to audit it.

## Deployment

Two paths:

### Local dev — uvicorn + compose dependencies

```powershell
docker compose up -d chromadb mlflow
alembic -c backend/persistence/alembic.ini upgrade head
uvicorn backend.main:app --reload --port 8000
streamlit run frontend/streamlit_app.py
```

### All-in-compose — production-ish

```powershell
docker compose up
```

Services:

| Service | Port (host) | Image / build | Volumes |
|---------|-------------|---------------|---------|
| `api` | 8000 | builds from `backend/Dockerfile` | `aura_data` → `/app/backend/data` |
| `frontend` | 8501 | builds from `frontend/Dockerfile` | source-mount `./frontend:/app` |
| `chromadb` | 8001 → 8000 | `chromadb/chroma:latest` | `chromadb_data` → `/chroma/chroma` |
| `mlflow` | 5000 | `ghcr.io/mlflow/mlflow:latest` | `mlflow_data` → `/mlflow` (SQLite store + artifact root) |

The `aura_data` named volume is the durability boundary — SQLite,
uploaded documents, and generated artifacts all live inside it.
Recreating containers without deleting the volume preserves state.

### Auth

By default the API has **no authentication** (single-trusted-operator
context per spec Assumptions). To gate it:

```ini
AURA_API_TOKEN=some-long-random-string
```

When set, every `/api/v1/*` call must include
`Authorization: Bearer some-long-random-string`. The frontend reads the
same env var from `AURA_API_TOKEN` (Docker compose passes it through).

This is **not** a multi-tenant boundary — brand isolation in retrieval
(FR-005) is for output quality, not access control. If you expose Aura
beyond a single operator, put it behind a real reverse proxy with proper
auth.

## Runtime knobs and what to tune

| Symptom | Knob to try |
|---------|-------------|
| End-to-end latency too high | Lower `AURA_RETRY_CAP` (fewer attempts on failure); inspect MLflow per-stage durations for the bottleneck |
| Critic too strict (rejects everything) | Lower `AURA_CRITIC_THRESHOLD` (default 0.7 → try 0.6) |
| Critic too lenient | Raise `AURA_CRITIC_THRESHOLD` |
| Too much queue wait under load | Raise `AURA_CONCURRENCY_CAP` (be mindful of OpenRouter rate limits) |
| Disk filling up with old runs | Lower `AURA_RUN_RETENTION_PER_BRAND` (default 100) |
| Want to test the degraded research path | Unset `TAVILY_API_KEY` and restart the API |

## Maintenance operations

### Reset all state (destructive)

```powershell
docker compose down -v          # drops the aura_data volume
# (re-bring-up will re-run migrations on first start)
```

### Browse the SQLite DB

```powershell
# Locally:
sqlite3 backend/data/aura.db
# Inside compose:
docker compose exec api sqlite3 /app/backend/data/aura.db
```

### Inspect ChromaDB

ChromaDB exposes its REST API on host port 8001. Per-brand collections
are named `brand_{brand_id}`. The Chroma admin endpoints (`/api/v1/`
inside the Chroma service) can list collections and document counts —
useful for confirming an ingest worked.

### Brand wipe

```powershell
curl -X DELETE http://localhost:8000/api/v1/brands/{brand_id}
```

Cascades to all documents, runs, traces, image artifacts, and the brand's
ChromaDB collection (FR-024). Document-level delete is intentionally not
exposed in v1 — the only escape hatch is brand wipe.

## Known gaps

- No multi-worker support — `CampaignRunner`'s `asyncio.Semaphore` and the FR-025 sweep both assume a single process owns the SQLite file. Scaling to multiple workers means a real broker (Redis, Celery) and per-row leases. Out of scope for v1.
- No structured log emission (JSON lines) — plain logging only. Logs are intended for human reading via `docker compose logs api`.
- No metrics endpoint — Prometheus/OTel are not wired. MLflow + the SQLite trace are the v1 observability surfaces.
- No GitHub Actions yet (PROJECT_MILESTONES.md §M4 calls for it). Locally, `ruff check . && ruff format --check . && mypy && pytest` is the equivalent gate.
