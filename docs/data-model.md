# Data model

This page summarises the data model. The authoritative source is
[`specs/001-aura-marketing-platform/data-model.md`](../specs/001-aura-marketing-platform/data-model.md);
the SQLAlchemy ORM in [`backend/persistence/models.py`](../backend/persistence/models.py)
is the source of truth for persisted state; the Pydantic models in
[`agents/schemas.py`](../agents/schemas.py) are the source of truth for
wire-format payloads.

## Three storage layers

| Layer | Backing | Holds | Why here |
|-------|---------|-------|----------|
| **Relational** | SQLite (`backend/data/aura.db`, SQLAlchemy 2.x + Alembic) | brands, documents, runs, stage_traces, campaign_outputs | ACID, foreign keys for the FR-024 brand-delete cascade, query layer for FR-022 retention pruning |
| **Vector** | ChromaDB (compose service, host port 8001) | per-brand collection `brand_{brand_id}` with embedded chunks | Structural brand isolation (FR-005); native vector similarity search |
| **Files** | Filesystem under `backend/data/` (mounted as `aura_data` volume in compose) | raw uploaded documents, generated PNG images | Avoid multi-MB BLOBs in SQLite and base64 in JSON (FR-023) |

## Entities at a glance

```text
                            ┌────────┐
                       1 ──►│ Brand  │◄── 1
                       │    └───┬────┘    │
                       │        │ * (cascade on delete)
                       │        ├──────────────┐
                       │        ▼              ▼
                  ┌────┴────┐ ┌────────┐  ┌──────┐
                  │Document │ │  Run   │  │ (Chroma  │
                  └────┬────┘ └───┬────┘  │  brand_  │
                       │          │       │   {id})   │
                       │ *        │ *      └──────────┘
                       │          │
                       ▼          ▼
                  (Chroma     ┌──────────────┐    ┌─────────────────┐
                   chunks)    │ StageTrace   │    │ CampaignOutput  │
                              │  (0..many)   │    │ (0..1 per run)  │
                              └──────────────┘    └─────────────────┘
```

| Entity | Purpose | Cardinality |
|--------|---------|-------------|
| **Brand** | Logical container for a customer. ULID id (immutable); free-text display name (mutable, no uniqueness). | one to many of everything else |
| **Document** | One uploaded file under a brand. Has format, byte_size, SHA-256 content_hash, chunk_count, parse_status. | many per brand |
| **Brand Knowledge Chunk** | Retrieval-ready unit of text in ChromaDB. Provenance: `{document_id}:{chunk_index}` + optional page. | many per document |
| **Run** | One execution of the pipeline for one brief. ULID id; status `queued`/`running`/`done`/`failed`; carries `retry_cap` + `critic_threshold` captured at submit time. | many per brand |
| **StageTrace entry** | One per `(run_id, stage, attempt)`. Inputs, outputs, duration, model calls, optional critic verdict, optional error. | many per run |
| **CampaignOutput** | Deliverable for `status='done'` runs. Headline, primary text, CTA, image path/dimensions, critic score breakdown. | 0..1 per run (absent for `failed`) |
| **CriticScore** | Embedded in CampaignOutput and in each critic-stage trace. Overall + four required dimensions + pass/fail + feedback. | — |

## Pydantic schemas (wire format)

In [`agents/schemas.py`](../agents/schemas.py). All use
`ConfigDict(extra="forbid")`. The most important ones:

| Schema | Used as |
|--------|---------|
| `CampaignRequest` | request body of `POST /campaigns`; carried through every stage |
| `ResearchOutput` | research stage output |
| `RetrievedContext` (with `Chunk`) | retrieval stage output |
| `AdCopy` | copy stage output, copied into `CampaignOutput.ad_copy` |
| `GeneratedImage` | image stage output; `path` is the API-relative URL `/api/v1/artifacts/{brand_id}/{run_id}.png` |
| `CriticScore` | critic stage output and `CampaignOutput.score` |
| `Campaign` | combined deliverable (request + ad_copy + image + score + run_id) |
| `RunRecord` | full run snapshot used by the runner and by MLflow logging |
| `StageTraceEntry` | one row of the audit trace |
| `ModelCall` | one external-call record inside a stage trace; provider ∈ `openrouter | huggingface | tavily` |
| `Brand`, `DocumentRecord` | response shapes from the brands and documents APIs |

The API response models in
[`backend/api/campaigns.py`](../backend/api/campaigns.py) (`Run`,
`RunSummary`, `CampaignOutput`) are FastAPI wrappers around these — same
field semantics, just shaped for the HTTP surface.

## SQLite schema

Authoritative DDL lives in the Alembic migrations under
[`backend/persistence/migrations/`](../backend/persistence/migrations/);
the ORM mappings are in [`backend/persistence/models.py`](../backend/persistence/models.py).
Summary:

### `brands`
- `id TEXT PRIMARY KEY` (ULID, immutable)
- `display_name TEXT NOT NULL` (1–200 chars, no uniqueness)
- `created_at`, `updated_at` (ISO-8601 UTC)

### `documents`
- `id TEXT PRIMARY KEY` (ULID)
- `brand_id TEXT FK → brands.id ON DELETE CASCADE`
- `original_filename`, `format` (`pdf|docx|txt|md`), `byte_size` (≤50MB)
- `content_hash TEXT` (SHA-256 hex, 64 chars)
- `chunk_count`, `parse_status` (`parsed|rejected`), `parse_error`, `storage_path`
- `UNIQUE (brand_id, content_hash)` — backs FR-004 dedup

### `runs`
- `id TEXT PRIMARY KEY` (ULID)
- `brand_id TEXT FK → brands.id ON DELETE CASCADE`
- `brief`, `platform`, `target_audience`, `status` (`queued|running|done|failed`)
- `current_stage`, `attempt_count`, `retry_cap`, `critic_threshold` (captured at submit time)
- `failed_stage`, `failed_reason` (set only when `status='failed'`)
- `submitted_at`, `started_at`, `completed_at`
- Indexes: `(brand_id, submitted_at DESC)` for FR-022 pruning; `(status)` for FR-025 sweep

### `stage_traces`
- `id INTEGER PRIMARY KEY AUTOINCREMENT`
- `run_id TEXT FK → runs.id ON DELETE CASCADE`
- `(run_id, attempt, stage) UNIQUE` — one row per stage per attempt
- `status` (`ok|degraded|failed`), `started_at`, `completed_at`, `duration_ms`
- `inputs_json`, `outputs_json`, `model_calls_json`, `verdict_json` (critic only), `error_message`

### `campaign_outputs`
- `run_id TEXT PRIMARY KEY FK → runs.id ON DELETE CASCADE` (one-to-one)
- `winning_attempt`, `headline`, `primary_text`, `cta`
- `image_path` (API-relative URL), `image_width`, `image_height`
- `final_score_overall`, `final_score_breakdown_json`, `final_score_passed`, `final_score_feedback`

## State machines

### Run lifecycle

```text
   ┌──────┐  runner picks up    ┌────────┐  graph done           ┌──────┐
   │queued├────────────────────►│running ├──────────────────────►│ done │
   └──┬───┘                     └────┬───┘                       └──────┘
      │                              │ stage hard-fail
      │  FR-025 sweep                ▼  (FR-018, FR-021)
      │  FR-024 brand delete  ┌────────┐
      └──────────────────────►│ failed │
                              └────────┘
```

`done` and `failed` are terminal. A `done` run always has a
`campaign_outputs` row; a `failed` run never does (FR-018). See
[architecture.md#run-state-machine](architecture.md#run-state-machine)
for transition triggers.

### Document parse lifecycle

```text
upload ─► size check ─► dedup check ─► persist file
              │             │
              ▼             ▼
       413 Too Large   409 Conflict (duplicate)
              │
              ▼
        parse + chunk + embed
              │
   ┌──────────┴──────────────┐
   ▼                          ▼
parse_status='parsed'       parse_status='rejected'
+ chunks in Chroma          + parse_error set
+ 201 to client             + 422 to client (file retained for audit)
```

A `rejected` document row is **retained** so operators can audit failed
uploads, and FR-004 dedup is keyed on `content_hash` regardless of parse
status — re-uploading a file that previously failed to parse is rejected
as a duplicate.

## Validation rules (cross-cutting)

| Rule | Where enforced |
|------|----------------|
| Brand display name 1–200 chars | Pydantic + SQL CHECK |
| Document format ∈ {pdf,docx,txt,md} | Pydantic `DocumentFormat` + SQL CHECK |
| Document byte_size ≤ 50 MB | Pydantic + SQL CHECK + ingest pre-check |
| `(brand_id, content_hash)` unique | SQL UNIQUE index |
| Platform ∈ supported set | Pydantic `Platform` + SQL CHECK |
| Critic `overall` ∈ [0,1] | Pydantic + SQL CHECK |
| Critic breakdown contains the four required dimensions | Critic stage code (post-LLM validation) |
| Brand delete cascades atomically | SQL `ON DELETE CASCADE` + orchestrator post-commit hook for Chroma + filesystem |
| Run pruning to ≤ `AURA_RUN_RETENTION_PER_BRAND` (default 100) | Orchestrator, fired after each terminal transition |
| Brand isolation on retrieval | Structural — per-brand Chroma collections; no shared-collection fallback |
| No image bytes in JSON | API contract — `image_url` only (FR-023) |

For the full table with FR citations, see
[`specs/.../data-model.md#validation-rules-and-constraints-cross-cutting`](../specs/001-aura-marketing-platform/data-model.md#validation-rules-and-constraints-cross-cutting).
