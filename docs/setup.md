# Setup

How to install dependencies, configure secrets, bring up the stack, and
verify it works. For the full operator walkthrough (create a brand,
upload a document, submit a brief), see
[`specs/.../quickstart.md`](../specs/001-aura-marketing-platform/quickstart.md).

## Prerequisites

- **Python 3.11** (pinned via `requires-python = "==3.11.*"` in [pyproject.toml](../pyproject.toml)). Earlier and later minor versions are unsupported.
- **Docker Desktop** (Windows / macOS) or Docker Engine + Compose (Linux), for the ChromaDB and MLflow services.
- **PowerShell** on Windows or bash/zsh on macOS/Linux. Commands below use PowerShell syntax — substitute `export FOO=bar` for `$env:FOO = "bar"` on POSIX shells.

## Install (editable, with dev + frontend extras)

From the repo root:

```powershell
pip install -e ".[dev,frontend]"
```

This installs `aura-depi` in editable mode, plus:

- `dev` extras — `ruff`, `mypy`, `pytest`, `httpx` (FastAPI TestClient).
- `frontend` extras — `streamlit`, `httpx`.

The four top-level packages (`agents`, `backend`, `rag`, `eval`) are
discovered automatically by `setuptools.packages.find` per
[pyproject.toml](../pyproject.toml).

## Configure secrets

Copy `.env.example` to `.env` and fill in real values:

```powershell
Copy-Item .env.example .env
notepad .env
```

### Required for live runs

| Env var | Source | Stage that needs it |
|---------|--------|---------------------|
| `OPENROUTER_API_KEY` | https://openrouter.ai/keys | `copy`, `image`, `critic` |
| `TAVILY_API_KEY` | https://app.tavily.com/ | `research` — **degradable**: leave unset to exercise the FR-021 degraded path |

### Optional overrides (defaults shown)

```ini
# Models
AURA_LLM_MODEL=openai/gpt-4o-mini
AURA_IMAGE_MODEL=google/gemini-2.5-flash-image-preview
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1

# Pipeline behaviour
AURA_CRITIC_THRESHOLD=0.7
AURA_RETRY_CAP=2
AURA_CONCURRENCY_CAP=5
AURA_RUN_RETENTION_PER_BRAND=100

# Storage + observability
AURA_DATA_DIR=backend/data
MLFLOW_TRACKING_URI=http://localhost:5000
MLFLOW_EXPERIMENT=aura
CHROMA_HOST=localhost
CHROMA_PORT=8001

# Optional bearer-token guard (single-trusted-operator default = off)
AURA_API_TOKEN=
```

See [operations.md](operations.md) for the full env-var reference with
semantics and ranges.

## Bring up the stack

Two ways: local processes for the API/frontend with compose for the
dependencies (best for iteration with reload-on-edit), or all four
services in compose.

### Option A — dev mode (recommended for development)

```powershell
# 1. Start ChromaDB + MLflow only
docker compose up -d chromadb mlflow

# 2. Run database migrations (creates backend/data/aura.db)
alembic -c backend/persistence/alembic.ini upgrade head

# 3. Window 1 — API
uvicorn backend.main:app --reload --port 8000

# 4. Window 2 — Streamlit frontend
$env:AURA_API_BASE = "http://localhost:8000"
streamlit run frontend/streamlit_app.py
```

The frontend opens at `http://localhost:8501`. The API serves OpenAPI
docs at `http://localhost:8000/docs` and the OpenAPI JSON at
`http://localhost:8000/openapi.json`.

### Option B — all-in-compose

```powershell
docker compose up
```

Brings up `api` (port 8000), `frontend` (8501), `chromadb` (8001 → 8000
inside the container), and `mlflow` (5000). The `backend/data/` tree is
mounted as a named Docker volume (`aura_data`), so SQLite state and
artifacts survive container recreation.

## Verify the install

```powershell
curl http://localhost:8000/api/v1/healthz
```

Expected (when all dependencies are reachable):

```json
{"status":"ok","version":"1.0.0","dependencies":{"sqlite":"ok","chromadb":"ok"}}
```

If `chromadb` reports `unreachable`, `docker compose ps` will show whether
the service is running. If you skipped migrations, the API will return
500 on the first DB-touching request — re-run `alembic upgrade head`.

## MLflow smoke

A no-op MLflow run against the configured tracking URI:

```powershell
python -m eval.smoke_mlflow
```

Open `http://localhost:5000` and look for an `aura` experiment with one
run. This is the same machinery the orchestrator uses to log each
campaign run as an MLflow experiment ([eval/tracking.py](../eval/tracking.py)).

## Lint / typecheck / test

```powershell
ruff check .                 # lints
ruff format --check .        # formatting
mypy                         # strict over backend, agents, rag, eval
pytest                       # full suite (deterministic stubs by default)
```

All four must pass before merging changes. See [testing.md](testing.md)
for the structure of the test suite and how to opt into live tests
against real OpenRouter / Tavily.

## Common setup pitfalls

| Symptom | Cause | Fix |
|---------|-------|-----|
| `ModuleNotFoundError: agents` | not installed editable | `pip install -e ".[dev,frontend]"` from the repo root |
| `sqlite3.OperationalError: no such table` | migrations not run | `alembic -c backend/persistence/alembic.ini upgrade head` |
| Healthz says chromadb `unreachable` | service not started, wrong host/port | `docker compose up -d chromadb`; check `CHROMA_HOST`/`CHROMA_PORT` |
| Campaign run stays `queued` forever | runner not started — usually means startup raised | check API logs around the lifespan; `CampaignRunner` is started in `_lifespan` ([backend/main.py:32](../backend/main.py#L32)) |
| Image stage fails 401 / 403 | OpenRouter key missing or out of credit | set `OPENROUTER_API_KEY`; verify on https://openrouter.ai/credits |
| Research stage marks `degraded` every time | Tavily key missing / over rate limit | set `TAVILY_API_KEY`, or leave unset to accept the degraded path |
| `alembic` not found | dev extras not installed | `pip install -e ".[dev,frontend]"` |
