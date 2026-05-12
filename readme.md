# Aura — Autonomous Multi-Agent Marketing Platform

A LangGraph-orchestrated pipeline that takes a campaign brief plus a
brand's own documents and produces a ready-to-publish social ad — copy
(headline + primary text + CTA) and a generated marketing image — grounded
in the brand's actual content and validated by a critic agent before
delivery.

This is the **DEPI graduation project**. See [ideas.md](ideas.md) for the
original proposal set (project #1 is the one being built); see
[PROJECT_MILESTONES.md](PROJECT_MILESTONES.md) for the milestone plan.

## What's here

| Read this if you want to … | Go here |
|---------------------------|---------|
| Understand what Aura is and why it exists | [docs/index.md](docs/index.md) |
| Install it and run it | [docs/setup.md](docs/setup.md) |
| Understand the architecture end-to-end | [docs/architecture.md](docs/architecture.md) |
| Understand the five agents and the LangGraph topology | [docs/agents.md](docs/agents.md) |
| Understand how brand documents become retrievable context | [docs/rag.md](docs/rag.md) |
| Read the full feature spec + design rationale | [specs/001-aura-marketing-platform/](specs/001-aura-marketing-platform/) |
| Walk through the system end-to-end as an operator | [specs/.../quickstart.md](specs/001-aura-marketing-platform/quickstart.md) |
| See the API contract | [docs/api.md](docs/api.md) — full OpenAPI at [contracts/openapi.yaml](specs/001-aura-marketing-platform/contracts/openapi.yaml) |

## Stack

- **Python 3.11** (pinned).
- **FastAPI + uvicorn** ([backend/main.py](backend/main.py)) for the HTTP API under `/api/v1`.
- **Streamlit** ([frontend/streamlit_app.py](frontend/streamlit_app.py)) for the operator UI.
- **LangGraph** ([agents/graph.py](agents/graph.py)) for the five-stage pipeline (research → retrieval → copy → image → critic) with a critic-driven retry loop.
- **SQLite + Alembic** ([backend/persistence/](backend/persistence/)) for run state, brand metadata, per-stage trace.
- **ChromaDB** ([rag/](rag/)) for per-brand vector collections — one collection per brand enforces FR-005 brand isolation structurally.
- **OpenRouter** for copy / image / critic LLM calls; **Tavily** for the degradable research stage; **sentence-transformers `all-MiniLM-L6-v2`** for embeddings (local, in-process).
- **MLflow** ([eval/](eval/)) for per-run experiment tracking (params, per-stage durations, critic dimension scores, artifacts).
- **Docker Compose** ([docker-compose.yml](docker-compose.yml)) for the full stack (api, frontend, chromadb, mlflow).

## Quickstart (60-second version)

```powershell
# 1. Install
pip install -e ".[dev,frontend]"

# 2. Configure secrets
Copy-Item .env.example .env
# Set OPENROUTER_API_KEY (required) and TAVILY_API_KEY (optional, degradable)

# 3. Bring up ChromaDB + MLflow; run migrations
docker compose up -d chromadb mlflow
alembic -c backend/persistence/alembic.ini upgrade head

# 4. Run the API + frontend in two windows
uvicorn backend.main:app --reload --port 8000
streamlit run frontend/streamlit_app.py
```

Frontend at http://localhost:8501. Full walkthrough in
[specs/.../quickstart.md](specs/001-aura-marketing-platform/quickstart.md).

## Project layout

```text
agents/             # LangGraph pipeline + the 5 stage nodes + Pydantic schemas
backend/            # FastAPI app (api/), orchestrator (orchestrator/), persistence (persistence/)
rag/                # Document ingest pipeline: parsers, chunking, embeddings, Chroma upsert
eval/               # MLflow logging + smoke benchmarks
frontend/           # Streamlit single-file app
specs/              # Spec Kit artifacts (spec, plan, research, data-model, contract, quickstart)
docs/               # Project documentation (this site)
infra/              # Reserved for deployment/IaC artifacts (currently empty)
```

## Status

Implemented through **Phase 6** (see recent commits). The full `/api/v1`
surface, the LangGraph pipeline, RAG ingest, SQLite persistence with
Alembic migrations, the restart sweeper, per-brand run retention, MLflow
tracking, and the Streamlit frontend are wired and tested. Smoke
benchmarks under [eval/benchmarks/](eval/benchmarks/) verify end-to-end
latency, brand grounding, concurrency, and repeatability against the
spec's success criteria (SC-001..SC-010).

## License

DEPI graduation project — TBD.
