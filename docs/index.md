# Aura — Project Documentation

**Aura** is an autonomous multi-agent marketing platform. It takes a short
campaign brief plus a brand's own documents and produces a ready-to-publish
social ad — copy (headline + primary text + CTA) and a generated marketing
image — grounded in the brand's actual content and validated by a critic
agent before delivery.

This is the documentation **site root**. It is written for two audiences:

| Reader | Start here |
|--------|-----------|
| **Developer / reviewer** cloning the repo to run or extend it | [setup.md](setup.md) → [architecture.md](architecture.md) → [agents.md](agents.md) |
| **DEPI evaluator / academic reader** assessing what was built and why | [architecture.md](architecture.md) → [agents.md](agents.md) → [evaluation.md](evaluation.md) |
| Anyone wanting the end-to-end walkthrough | [`specs/001-aura-marketing-platform/quickstart.md`](../specs/001-aura-marketing-platform/quickstart.md) |

## Table of contents

- [setup.md](setup.md) — install, env vars, run locally, run via Docker Compose, database migrations.
- [architecture.md](architecture.md) — system architecture: API ↔ orchestrator ↔ LangGraph ↔ storage layers; restart and concurrency semantics.
- [agents.md](agents.md) — the five-stage pipeline (research → retrieval → copy → image → critic), the retry loop, and per-stage I/O schemas.
- [rag.md](rag.md) — document ingest pipeline: parsing, chunking, embedding, per-brand ChromaDB collections.
- [data-model.md](data-model.md) — entities, SQLite schema, ChromaDB layout, filesystem layout, state transitions.
- [api.md](api.md) — at-a-glance API table (full contract in [`specs/.../contracts/openapi.yaml`](../specs/001-aura-marketing-platform/contracts/openapi.yaml)).
- [evaluation.md](evaluation.md) — MLflow tracking, critic dimensions, smoke benchmarks, mapping to spec success criteria.
- [operations.md](operations.md) — runtime config, observability, deployment, retention pruning, restart sweep.
- [testing.md](testing.md) — pytest layout, live test gate, deterministic stubs, the benchmark harness.

## Adjacent reference material (outside `docs/`)

- [`CLAUDE.md`](../CLAUDE.md) — code-assistant guidance for working in this repo (stack, conventions, known gaps).
- [`PROJECT_MILESTONES.md`](../PROJECT_MILESTONES.md) — the original DEPI milestone plan (M1 data → M5 demo).
- [`specs/001-aura-marketing-platform/`](../specs/001-aura-marketing-platform/) — the full Spec Kit set:
  - [`spec.md`](../specs/001-aura-marketing-platform/spec.md) — functional requirements (FR-001..FR-025), success criteria (SC-001..SC-010), edge cases.
  - [`plan.md`](../specs/001-aura-marketing-platform/plan.md) — implementation plan.
  - [`research.md`](../specs/001-aura-marketing-platform/research.md) — technology decisions with rationale.
  - [`data-model.md`](../specs/001-aura-marketing-platform/data-model.md) — full data model (this site's [data-model.md](data-model.md) is a condensed summary).
  - [`quickstart.md`](../specs/001-aura-marketing-platform/quickstart.md) — operator walkthrough.
  - [`tasks.md`](../specs/001-aura-marketing-platform/tasks.md) — implementation task list.
  - [`contracts/openapi.yaml`](../specs/001-aura-marketing-platform/contracts/openapi.yaml) — OpenAPI 3.1 contract.

## What Aura does in one paragraph

The operator picks a brand, writes a brief (free-text description, target
platform, target audience), and submits it. The submit call returns a
`run_id` in under a second; the run executes asynchronously. Behind the
scenes a LangGraph state machine runs five stages — market research
(degradable), brand-knowledge retrieval, copywriting, image generation, and
a critic verdict — with research and retrieval running in parallel. If the
critic rejects a draft, the system feeds the feedback back into the copy and
image stages and retries up to a configurable cap; the highest-scoring
attempt is delivered as the final campaign. Every run produces a per-stage
audit trace and an MLflow record.

## Status

The project is implemented through **Phase 6** (see recent commits and
[`PROJECT_MILESTONES.md`](../PROJECT_MILESTONES.md)). The full `/api/v1`
surface, the LangGraph pipeline, RAG ingest, SQLite persistence with
Alembic migrations, the restart sweeper (FR-025), per-brand run retention
(FR-022), MLflow tracking, and the Streamlit frontend are wired and tested.
Smoke benchmarks for end-to-end latency, brand grounding, concurrency, and
repeatability live under [`eval/benchmarks/`](../eval/benchmarks/).
