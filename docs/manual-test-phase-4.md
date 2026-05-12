# Manual Test — Phase 4 (User Story 2)

End-to-end test plan for the brand-onboarding and document-grounding work in Phase 4: create a brand, upload PDF/DOCX/TXT/MD, watch chunks land in a brand-scoped Chroma collection, and confirm a campaign run grounds its copy in those documents. Targets the routes from [contracts/openapi.yaml](../specs/001-aura-marketing-platform/contracts/openapi.yaml) and the success criteria in [spec.md](../specs/001-aura-marketing-platform/spec.md) (FR-002 / FR-003 / FR-004 / FR-005 / FR-012 / FR-024 / SC-003 / SC-004).

**Tasks covered**: T037–T053 (the full Phase 4 / US2 batch).

All commands use **PowerShell** on Windows (the project's primary shell). Run them from the repo root.

If you haven't already done the [Phase 3 walkthrough](manual-test-phase-3.md), do §1.1–§1.4 of that doc first to install deps, configure `.env`, bring up Chroma + MLflow, and apply the migration. This doc picks up from a working API.

---

## 0. Prerequisites

- Python 3.11 (`python --version` → `3.11.x`)
- Docker Desktop running, with `aura-chromadb` and `aura-mlflow` `Up`
- An OpenRouter API key for [§6](#6-brand-grounded-campaign-end-to-end) only — earlier sections (brand CRUD, document upload, isolation, cascade) need **no LLM keys**.
- (Optional) A Tavily API key. Leave blank to exercise the degradable-research path.

---

## 1. Reset state for a clean run

Phase 4 introduces the **brands API**, so the Phase 3 seed script (`backend.scripts.seed_test_brand`) is no longer the canonical way to create a brand — though it still works for backwards compatibility with the Phase 3 walkthrough.

To get a fully clean slate:

```powershell
# Stop uvicorn if it's running, then:
Remove-Item backend\data\aura.db* -ErrorAction SilentlyContinue
Remove-Item -Recurse -ErrorAction SilentlyContinue backend\data\uploads\*
Remove-Item -Recurse -ErrorAction SilentlyContinue backend\data\artifacts\*

# Reapply migrations
alembic -c backend/persistence/alembic.ini upgrade head
```

Wipe the embedded Chroma store too if you want isolation between phase walkthroughs:

```powershell
docker compose down chromadb
docker volume rm aura-depi_chromadb_data    # name varies by repo dir
docker compose up -d chromadb
```

---

## 2. Start the API

In a dedicated terminal:

```powershell
uvicorn backend.main:app --reload --port 8000
```

Watch the startup logs for:

- `interrupt sweeper completed`
- `CampaignRunner started`
- `Application startup complete.`

Health check:

```powershell
curl.exe -s http://localhost:8000/api/v1/healthz | ConvertFrom-Json
```

Expected:

```json
{ "status": "ok", "components": { "database": "ok", "chromadb": "ok" } }
```

> **`chromadb: unreachable`?** Same fix as the Phase 3 doc — restart uvicorn after editing `.env`, or set `$env:CHROMA_HOST = "localhost"; $env:CHROMA_PORT = "8001"` inline before launching it.

---

## 3. Brand lifecycle (T050)

> **PowerShell + `curl` gotcha** (recap from Phase 3): the built-in `curl` alias points to `Invoke-WebRequest`, which mangles JSON bodies. Use `curl.exe` explicitly, or use `Invoke-RestMethod`. Both are shown for the first call; later sections default to `Invoke-RestMethod`.

### 3.1. Create a brand

Option A — `curl.exe`:

```powershell
'{"display_name":"ACME Sneakers"}' | curl.exe -s -X POST http://localhost:8000/api/v1/brands `
  -H "Content-Type: application/json" -d '@-'
```

Option B — `Invoke-RestMethod` (recommended):

```powershell
$brand = Invoke-RestMethod -Method POST -Uri http://localhost:8000/api/v1/brands `
  -ContentType "application/json" `
  -Body (@{ display_name = "ACME Sneakers" } | ConvertTo-Json)

$brand
$brandId = $brand.id    # ULID; save this for the rest of the walkthrough
$brandId
```

Expected (HTTP 201):

```json
{
  "id": "01K…",
  "display_name": "ACME Sneakers",
  "created_at": "2026-05-06T…Z",
  "updated_at": "2026-05-06T…Z"
}
```

### 3.2. List + fetch by id

```powershell
Invoke-RestMethod http://localhost:8000/api/v1/brands             # newest first
Invoke-RestMethod "http://localhost:8000/api/v1/brands/$brandId"  # 200
```

### 3.3. Negative cases

```powershell
# 404 unknown id
curl.exe -i http://localhost:8000/api/v1/brands/01HXNOSUCHBRAND00000000000

# 422 missing display_name
curl.exe -i -X POST http://localhost:8000/api/v1/brands `
  -H "Content-Type: application/json" -d '{}'

# 422 blank display_name
curl.exe -i -X POST http://localhost:8000/api/v1/brands `
  -H "Content-Type: application/json" -d '{"display_name":""}'

# 422 extra field rejected (extra="forbid" on Pydantic schemas)
curl.exe -i -X POST http://localhost:8000/api/v1/brands `
  -H "Content-Type: application/json" -d '{"display_name":"X","id":"client-set"}'
```

---

## 4. Document upload (T046–T049, T051)

Make a tiny brand-distinctive text file. The marker phrase (`ACME-MESH-PRO-7421`) is what we'll later look for in the grounded copy.

```powershell
@"
ACME Sneakers Brand Voice
Distinctive marker: ACME-MESH-PRO-7421.
Free shipping over `$75 in the contiguous US.
Ships next day.
"@ | Set-Content .\acme-brand.txt -Encoding UTF8
```

### 4.1. Happy path — upload + verify

```powershell
$doc = curl.exe -s -X POST "http://localhost:8000/api/v1/brands/$brandId/documents" `
    -F "file=@.\acme-brand.txt" | ConvertFrom-Json
$doc
```

Expected (HTTP 201):

```json
{
  "id": "01K…",
  "brand_id": "01K…",
  "original_filename": "acme-brand.txt",
  "format": "txt",
  "byte_size": 121,
  "content_hash": "9c3f…64-hex…",
  "chunk_count": 1,
  "parse_status": "parsed",
  "parse_error": null,
  "created_at": "…"
}
```

Spot-check the disk:

```powershell
ls backend\data\uploads\$brandId
```

You should see `{document_id}.txt` — the verbatim file the API persisted.

### 4.2. List documents for the brand

```powershell
Invoke-RestMethod "http://localhost:8000/api/v1/brands/$brandId/documents" |
  ConvertTo-Json -Depth 3
```

Sorted newest first.

### 4.3. Edge cases

**409 duplicate** (FR-004 dedup is per `(brand_id, content_hash)`):

```powershell
curl.exe -i -X POST "http://localhost:8000/api/v1/brands/$brandId/documents" `
  -F "file=@.\acme-brand.txt"
# expect HTTP/1.1 409
```

**415 unsupported type**:

```powershell
"hello" | Set-Content .\notes.xlsx
curl.exe -i -X POST "http://localhost:8000/api/v1/brands/$brandId/documents" `
  -F "file=@.\notes.xlsx"
# expect HTTP/1.1 415
```

**413 oversize** (50 MB cap):

```powershell
# Generate a 51 MB junk file (takes a moment)
[System.IO.File]::WriteAllBytes(".\big.txt", (New-Object byte[] (52428801)))
curl.exe -i -X POST "http://localhost:8000/api/v1/brands/$brandId/documents" `
  -F "file=@.\big.txt"
# expect HTTP/1.1 413
Remove-Item .\big.txt
```

**422 unparseable** (whitespace-only):

```powershell
"   `n   `n" | Set-Content .\blank.txt -Encoding UTF8
curl.exe -i -X POST "http://localhost:8000/api/v1/brands/$brandId/documents" `
  -F "file=@.\blank.txt"
# expect HTTP/1.1 422 — but the *row* is still inserted with parse_status='rejected'
Invoke-RestMethod "http://localhost:8000/api/v1/brands/$brandId/documents" |
  Where-Object parse_status -eq "rejected"
```

The rejected row's file is retained on disk for audit (`backend/data/uploads/{brand_id}/{document_id}.txt`).

**404 unknown brand**:

```powershell
curl.exe -i "http://localhost:8000/api/v1/brands/01HXNOSUCHBRAND00000000000/documents"
```

---

## 5. PDF / DOCX upload (optional)

If you have a real PDF or DOCX brand guide, upload it the same way:

```powershell
curl.exe -s -X POST "http://localhost:8000/api/v1/brands/$brandId/documents" `
  -F "file=@.\path\to\acme_brand_guide.pdf" | ConvertFrom-Json
```

The PDF parser (`pdfplumber`) extracts text-layer content only. **Image-only PDFs are rejected** with HTTP 422 and `parse_status='rejected'`. DOCX uses `python-docx`. Markdown is read as UTF-8 text.

`chunk_count` will be roughly `ceil(token_count / 500)` — a 5-page text-PDF brand guide typically lands 5–15 chunks.

---

## 6. Brand-grounded campaign (end-to-end)

This is the payoff for User Story 2: a campaign whose copy references content from the uploaded document. Needs an OpenRouter key.

### 6.1. Set up keys

In `.env`:

```ini
OPENROUTER_API_KEY=sk-or-<your-key>
AURA_LLM_MODEL=openai/gpt-4o-mini
AURA_IMAGE_MODEL=google/gemini-2.5-flash-image-preview
TAVILY_API_KEY=        # blank → degraded research is fine
```

Restart `uvicorn` so the new env loads.

### 6.2. Submit the brief

```powershell
$resp = Invoke-RestMethod -Method POST -Uri http://localhost:8000/api/v1/campaigns `
  -ContentType "application/json" `
  -Body (@{
      brief = "Promote our running shoes to fitness-curious city dwellers. Lead with breathability and free shipping."
      platform = "instagram"
      brand_id = $brandId
      target_audience = "18-24 fitness-curious"
  } | ConvertTo-Json)

$runId = $resp.run_id
$runId
```

Expected: HTTP 202, `status: "queued"`, returned in **< 1s** (SC-002).

### 6.3. Poll until terminal

```powershell
do {
    Start-Sleep -Milliseconds 500
    $run = Invoke-RestMethod "http://localhost:8000/api/v1/campaigns/$runId"
    "{0,-8} progress={1:P0} stage={2}" -f $run.status, $run.progress, $run.current_stage
} while ($run.status -notin @("done","failed"))

$run.output.ad_copy
$run.output.image_url
```

### 6.4. Verify grounding (FR-012, partial SC-003)

The headline or primary text should reference content from `acme-brand.txt`:

```powershell
$blob = ($run.output.ad_copy.headline + " " + $run.output.ad_copy.primary_text).ToLower()
"acme matched:        $($blob -match 'acme')"
"mesh pro matched:    $($blob -match 'mesh pro')"
"shipping matched:    $($blob -match 'shipping')"
```

At least one of these should be `True`. If all three are `False`, inspect the `retrieval` stage's trace to see whether your chunk was actually retrieved:

```powershell
$run.trace | Where-Object stage -eq retrieval | ConvertTo-Json -Depth 4
```

The retrieval stage's `model_calls[0].op` should read `similarity-search` (not `empty-collection-skip`).

### 6.5. View the generated image

```powershell
Start-Process "http://localhost:8000$($run.output.image_url)"
```

The PNG lives under `backend/data/artifacts/{brand_id}/{run_id}.png` (FR-023).

---

## 7. Brand isolation (FR-005, SC-004)

Create a second brand and upload a document with a *different* marker phrase. Then run a campaign against the *first* brand and confirm none of brand B's chunks leaked.

```powershell
# Brand B
$brand2 = Invoke-RestMethod -Method POST -Uri http://localhost:8000/api/v1/brands `
  -ContentType "application/json" -Body (@{ display_name = "Globex Audio" } | ConvertTo-Json)
$brandId2 = $brand2.id

@"
Globex Audio Brand Voice
Distinctive marker: GLOBEX-SOUND-9999.
Studio-grade headphones for serious listeners.
"@ | Set-Content .\globex-brand.txt -Encoding UTF8

curl.exe -s -X POST "http://localhost:8000/api/v1/brands/$brandId2/documents" `
  -F "file=@.\globex-brand.txt" | ConvertFrom-Json
```

Now submit a campaign for **brand A** and inspect retrieval:

```powershell
$resp = Invoke-RestMethod -Method POST -Uri http://localhost:8000/api/v1/campaigns `
  -ContentType "application/json" `
  -Body (@{
      brief = "What is your distinctive product marker phrase?"
      platform = "instagram"
      brand_id = $brandId
      target_audience = "any"
  } | ConvertTo-Json)

do {
    Start-Sleep -Milliseconds 500
    $run = Invoke-RestMethod "http://localhost:8000/api/v1/campaigns/$($resp.run_id)"
} while ($run.status -notin @("done","failed"))

# Inspect retrieval outputs (read straight from the SQLite trace if needed)
$run.trace | Where-Object stage -eq retrieval | ConvertTo-Json -Depth 4

# The delivered copy must NOT mention GLOBEX-SOUND-9999
$blob = ($run.output.ad_copy.headline + " " + $run.output.ad_copy.primary_text)
"globex leaked: $($blob -match 'GLOBEX')"   # expect False
```

For the formal 100-call test, the automated suite has it:

```powershell
pytest backend/tests/test_brand_isolation.py -v
```

---

## 8. Brand delete cascade (T050, FR-024)

Delete brand A and verify the cascade hits SQL, ChromaDB, and the filesystem.

```powershell
# Pre-state
ls backend\data\uploads\$brandId
ls backend\data\artifacts\$brandId -ErrorAction SilentlyContinue
Invoke-RestMethod "http://localhost:8000/api/v1/campaigns?brand_id=$brandId&limit=10"

# Delete
curl.exe -i -X DELETE "http://localhost:8000/api/v1/brands/$brandId"
# expect HTTP/1.1 204

# Cascade verification
curl.exe -i "http://localhost:8000/api/v1/brands/$brandId"               # 404
curl.exe -i "http://localhost:8000/api/v1/brands/$brandId/documents"     # 404
ls backend\data\uploads\$brandId  -ErrorAction SilentlyContinue          # gone
ls backend\data\artifacts\$brandId -ErrorAction SilentlyContinue         # gone
Invoke-RestMethod "http://localhost:8000/api/v1/campaigns?brand_id=$brandId"  # []
```

The brand-deleted Chroma collection is dropped too — verify with the embedded chroma client if you want the deepest sanity check, otherwise trust the automated test:

```powershell
pytest backend/tests/test_brand_delete_cascade.py -v
```

**In-flight cancel** (FR-024, deleting a brand mid-campaign): the brands API marks every non-terminal run as `failed` with `failed_reason='brand_deleted'` before cascading, then the FK cascade removes the rows. Verified end-to-end by:

```powershell
pytest backend/tests/test_brand_delete_cancels_inflight.py -v
```

---

## 9. Streamlit UI walkthrough (T053)

In another terminal:

```powershell
$env:AURA_API_BASE = "http://localhost:8000"
streamlit run frontend/streamlit_app.py
```

Open http://localhost:8501 and walk the four screens:

1. **Brands** — create, list, delete a throwaway brand. Phase 4 added this screen; Phase 3 had no UI brand management.
2. **Documents** — pick a brand from the dropdown, drag-drop a file, watch the table populate; try a duplicate to see the inline duplicate warning.
3. **Campaign request** — pick the brand from the dropdown (no more free-text `brand_id` field), submit a brief.
4. **Results** — same as Phase 3, but `image_url` now resolves correctly because the campaign was scoped to a real brand.

---

## 10. Run the automated Phase 4 suite

If you'd rather skip manual API calls, every Phase 4 task has an automated test that runs offline (stubbed external clients, embedded ChromaDB, per-test SQLite):

```powershell
pytest backend/tests/test_brands_contract.py `
       backend/tests/test_documents_contract.py `
       backend/tests/test_brand_isolation.py `
       backend/tests/test_brand_delete_cascade.py `
       backend/tests/test_brand_delete_cancels_inflight.py `
       backend/tests/test_grounded_copy_integration.py `
       rag/tests/ -v
```

Expected: **38 passed** in ~25s. No API spend.

The whole project suite (Phase 1 + Phase 2 + Phase 3 + Phase 4) should also be green:

```powershell
ruff check .
ruff format --check .
mypy
pytest
# expect: 89 passed
```

---

## 11. Cleanup

```powershell
docker compose down                              # stops chromadb + mlflow (keeps volumes)
docker compose down -v                           # also wipes Chroma + MLflow data
Remove-Item backend\data\aura.db*                # reset SQLite (re-run alembic + create brands)
Remove-Item -Recurse backend\data\uploads\*      # purge uploaded documents
Remove-Item -Recurse backend\data\artifacts\*    # purge generated images
Remove-Item .\acme-brand.txt, .\globex-brand.txt, .\notes.xlsx, .\blank.txt -ErrorAction SilentlyContinue
```

---

## What's covered by this test

| Spec / acceptance criterion | Verified in |
|---|---|
| **FR-001** — brands minted with system ULID | §3.1 |
| **FR-002** — accept PDF / DOCX / TXT / MD; reject others | §4.1, §4.3, §5 |
| **FR-003** — per-brand document storage + listing | §4.1, §4.2 |
| **FR-004** — duplicate-content rejection per brand (SHA-256) | §4.3 (409 case) |
| **FR-005** — brand isolation (no cross-brand chunk leak) | §7 + automated 100-call test |
| **FR-012** — grounded copy references brand documents | §6.4 |
| **FR-024** — brand delete cascades across SQL, Chroma, filesystem | §8 |
| **FR-024** (in-flight) — running campaigns cancelled cleanly on brand delete | automated test |
| **SC-003** — ≥ 80 % of campaigns reference brand facts (partial check) | §6.4 (one campaign); formal eval lives in `eval/benchmarks/brand_grounding_smoke.py` (T072) |
| **SC-004** — zero leak across brands over many retrievals | automated `test_brand_isolation.py` (100 calls) |
| **T046–T049** — RAG parsers / chunker / embeddings / ingest | §4 + §5 + `rag/tests/` |
| **T050** — brands API CRUD + cascade | §3 + §8 |
| **T051** — documents API upload + list with full error mapping | §4 |
| **T052** — brand-scoped retrieval populated | §6.4 (similarity-search trace), §7 |
| **T053** — Streamlit Brands + scoped Documents screens | §9 |
