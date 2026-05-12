# RAG — Document ingestion and retrieval

Aura's retrieval-augmented generation pipeline grounds copy in the
**brand's own documents** rather than generic invention. This page
documents how documents are ingested, chunked, and embedded, how the
retrieval stage queries the resulting corpus at run time, and how brand
isolation (FR-005) is enforced.

## The two halves

```text
                INGEST (one-time, per document upload)
   ┌─────────────────────────────────────────────────────────┐
   │ POST /api/v1/brands/{brand_id}/documents                 │
   │   ─► hash → dedup check → size check                     │
   │   ─► save raw bytes → parse → chunk → embed              │
   │   ─► upsert into ChromaDB collection brand_{brand_id}    │
   │   ─► INSERT documents row (parse_status='parsed')        │
   └─────────────────────────────────────────────────────────┘

                RETRIEVAL (every campaign run, parallel with research)
   ┌─────────────────────────────────────────────────────────┐
   │ agents.stages.retrieval.run_retrieval(...)               │
   │   ─► resolve brand_id → collection name brand_{id}       │
   │   ─► embed query → top-K similarity query                │
   │   ─► return RetrievedContext { chunks, brand_voice }     │
   └─────────────────────────────────────────────────────────┘
```

## Ingest pipeline

Code: [`rag/ingest.py`](../rag/ingest.py). Called from
[`backend/api/documents.py`](../backend/api/documents.py) on each upload.

### Step 1 — hash and dedup (FR-004)

The uploaded bytes are SHA-256 hashed. The `(brand_id, content_hash)`
pair is looked up in the `documents` table. If it exists, the upload is
rejected with `409 Conflict` and `code=duplicate_document`. **Dedup is
per-brand** — the same file uploaded to two different brands is fine and
intentional (Brand A's brand guide is unrelated to Brand B's).

### Step 2 — size check

Files larger than **50 MB** (`52_428_800` bytes) are rejected with `413
Payload Too Large`. Empty files are rejected as
`DocumentParseError("file is empty")`. The cap is from
spec Assumptions.

### Step 3 — persist raw bytes

The file is written verbatim to
`backend/data/uploads/{brand_id}/{document_id}.{ext}`. The
`{document_id}` is a freshly minted ULID; the extension is one of `pdf`,
`docx`, `txt`, `md`. The directory is created lazily on the first
upload for the brand.

### Step 4 — parse

Format-specific adapters in [`rag/parsers.py`](../rag/parsers.py):

| Format | Library | Notes |
|--------|---------|-------|
| `pdf` | `pdfplumber` | Layout-aware text extraction. Image-only PDFs without a text layer are rejected (no OCR in v1). |
| `docx` | `python-docx` | Canonical choice. |
| `txt` | `Path.read_text(encoding="utf-8")` with `chardet` fallback for non-UTF-8 | |
| `md` | Same as `txt` | Markdown stays as raw text — embeddings handle it fine. |

Parsers raise `DocumentParseError` on failure. The orchestrator catches
it, INSERTs the `documents` row with `parse_status='rejected'` and the
human-readable `parse_error`, retains the file on disk for audit, and
re-raises so the API can return `422 Unprocessable Entity`.

### Step 5 — chunk

Code: [`rag/chunking.py`](../rag/chunking.py).

Token-based chunking with the **`cl100k_base` tokenizer** (the OpenAI
tokenizer used by GPT-4o-mini). Defaults: **~500-token chunks with
50-token overlap**. Per-chunk metadata:

- `text` — the chunk's text
- `source` — `"{document_id}:{chunk_index}"` — provenance back to the source document
- `chunk_index` — 0-based ordinal within the document
- `page` — set when the parser supplied page boundaries (PDFs); `None` otherwise

The 500/50 numbers are from [`research.md §9`](../specs/001-aura-marketing-platform/research.md) — well-tuned for marketing
content (paragraphs, bullet lists, short sections) without slicing
through claim-bearing sentences. Smaller chunks improve precision at the
cost of collection size; the values are tunable but not currently
env-driven.

### Step 6 — embed

Code: [`rag/embeddings.py`](../rag/embeddings.py).

Local **`sentence-transformers/all-MiniLM-L6-v2`** model (384 dimensions,
~90 MB on first load). Embedding runs in-process on the CPU — no
outbound API call per chunk, no GPU required. In `ModelCall` records
this is reported as `provider='huggingface'` even though it's a local
call (the `huggingface` provider literal covers any HF-derived model
regardless of network).

### Step 7 — upsert into ChromaDB

Each chunk is upserted into the brand's collection
`brand_{brand_id}` (created on first upload for the brand). Vector
metadata fields:

| Field | Type | Purpose |
|-------|------|---------|
| `document_id` | string | join back to `documents.id` |
| `chunk_index` | int | 0-based ordinal within the document |
| `source_filename` | string | denormalised for trace/debug readability |
| `page` | int | PDF page hint; `-1` when not applicable (Chroma metadata can't be null) |

Distance metric: cosine (Chroma default).

### Step 8 — persist `documents` row

A `documents` row is INSERTed with `parse_status='parsed'` and the final
`chunk_count`. The endpoint returns `201` with the `DocumentRecord`
payload.

## Retrieval

Code: [`agents/stages/retrieval.py`](../agents/stages/retrieval.py).

The retrieval stage receives the `CampaignRequest` and the
`chroma_client` from the graph state, resolves `brand_id` to the
collection name `brand_{brand_id}`, embeds a query built from the brief
and target audience, and returns the top-K matches.

Output: `RetrievedContext`:

- `chunks: list[Chunk]` — each with `text` and `source` (provenance back to `{document_id}:{chunk_index}`).
- `brand_voice: str` — a synthesised one-line summary of brand tone derived from the chunks; "" when the brand has no documents.

## Brand isolation (FR-005)

The architectural choice that makes brand isolation a *structural*
guarantee rather than a discipline:

> One ChromaDB collection per brand, named `brand_{brand_id}`.

There is no API path through the orchestrator or the stages that
queries a collection other than the requested brand's. The retrieval
stage takes `brand_id` from `CampaignRequest`, resolves the collection
name deterministically, and queries that collection. A forgotten
`where`-clause **cannot** leak cross-brand data because no shared
collection exists. This is materially stronger than a single shared
collection with a `where={"brand_id": ...}` filter, which turns brand
isolation into "don't forget the filter" — a class of bug that would
surface as a security-class incident.

The contract is also defended in tests under
[`rag/tests/`](../rag/tests/) and end-to-end in
[`eval/benchmarks/brand_grounding_smoke.py`](../eval/benchmarks/brand_grounding_smoke.py).

## Documents are append-only in v1

There is **no** `DELETE /api/v1/brands/{brand_id}/documents/{document_id}`
and no `PUT /.../documents/{document_id}` (spec FR-024 + Assumptions).
The only escape hatch is `DELETE /api/v1/brands/{brand_id}` which
cascades. This keeps the retrieval surface predictable for v1; document-
level mutation is a candidate for a later milestone.

## Configuration

| Env var | Default | What it tunes |
|---------|---------|---------------|
| `CHROMA_HOST` | `localhost` (dev) / `chromadb` (compose) | ChromaDB host |
| `CHROMA_PORT` | `8001` (dev) / `8000` (compose) | ChromaDB port |
| `AURA_DATA_DIR` | `backend/data` | Root for uploads + artifacts + `aura.db` |

When `CHROMA_HOST`/`CHROMA_PORT` aren't set (e.g. in tests), the runner
falls back to `chromadb.EphemeralClient()` ([runner.py:68](../backend/orchestrator/runner.py#L68))
— an in-process Chroma instance that lives only for the test's lifetime.
