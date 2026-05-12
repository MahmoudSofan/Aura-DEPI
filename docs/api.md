# Aura API Contract

The authoritative API contract lives in
[`specs/001-aura-marketing-platform/contracts/openapi.yaml`](../specs/001-aura-marketing-platform/contracts/openapi.yaml).

Open that file in any OpenAPI viewer (Swagger UI, Redoc, the FastAPI
`/docs` endpoint when running `uvicorn backend.main:app`) for the full
list of routes, schemas, and response shapes.

This page used to contain a hand-maintained mirror of the contract; that
mirror diverged from the live API and has been removed in favour of the
single OpenAPI source of truth (resolves the divergence flagged in
[CLAUDE.md](../CLAUDE.md)).

## At a glance

Base URL: `/api/v1`

| Resource    | Verb + path                                 | Purpose                                           |
| ----------- | ------------------------------------------- | ------------------------------------------------- |
| Health      | `GET  /healthz`                             | Liveness + dependency status (200 / 503).         |
| Brands      | `POST /brands`                              | Mint a brand (system-assigned ULID id).           |
|             | `GET  /brands`                              | List brands, newest first.                        |
|             | `GET  /brands/{brand_id}`                   | Fetch one brand.                                  |
|             | `DELETE /brands/{brand_id}`                 | Cascade-delete brand + docs + runs + artifacts.   |
| Documents   | `POST /brands/{brand_id}/documents`         | Upload a doc (multipart `file=`); ingests to RAG. |
|             | `GET  /brands/{brand_id}/documents`         | List docs for the brand, newest first.            |
| Campaigns   | `POST /campaigns`                           | Submit a brief; returns 202 + `run_id`.           |
|             | `GET  /campaigns`                           | List runs (filter by `brand_id`/`status`).        |
|             | `GET  /campaigns/{run_id}`                  | Poll a run; full output once `status='done'`.     |
| Artifacts   | `GET  /artifacts/{brand_id}/{filename}`     | Stream the generated PNG (200, `image/png`).      |

## Legacy redirects

The original unversioned routes (`/api/campaigns/generate`,
`/api/campaigns/{run_id}/status`, `/api/documents/upload`) emit `308 →
/api/v1/...` redirects (or `410 Gone` for the documents upload, which
moved under `/brands/{brand_id}`). See
[`backend/main.py`](../backend/main.py) for the exact wiring.

## Conventions

- **Auth** — operator API token via `Authorization: Bearer ...` when
  `AURA_API_TOKEN` is set; no auth when blank (single-trusted-operator
  default).
- **IDs** — brands and runs use ULIDs (string).
- **Errors** — standard FastAPI shape `{ "detail": "..." }`. Contract
  responses for 404 / 409 / 413 / 415 / 422 / 503 documented in the
  OpenAPI file.
- **Status enum (runs)** — `queued` → `running` → `done | failed`.
