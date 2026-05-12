# Manual Test — Phase 3 (User Story 1, MVP)

End-to-end test plan for the Aura MVP: submit a brief, get back ad copy + a generated image scored by the critic. Targets the routes from [contracts/openapi.yaml](../specs/001-aura-marketing-platform/contracts/openapi.yaml) and the success criteria in [spec.md](../specs/001-aura-marketing-platform/spec.md) (SC-001 / SC-002 / SC-005 / SC-008).

All commands use **PowerShell** on Windows (the project's primary shell). Run them from the repo root.

---

## 0. Prerequisites

- Python 3.11 (`python --version` → `3.11.x`)
- Docker Desktop running
- An OpenRouter API key — https://openrouter.ai/settings/keys
- (Optional) A Tavily API key — https://app.tavily.com/. Leave blank to exercise the degradable-research path.

---

## 1. One-time setup

### 1.1. Install the package (editable, with dev + frontend extras)

```powershell
python -m pip install -e ".[dev,frontend]"
```

Use `python -m pip` to avoid pip resolving against a different interpreter (Python 3.14 etc.).

### 1.2. Create your `.env`

```powershell
Copy-Item .env.example .env
```

Then open `.env` and set at minimum:

```ini
OPENROUTER_API_KEY=sk-or-<your-key>
CHROMA_HOST=localhost
CHROMA_PORT=8001
TAVILY_API_KEY=        # leave blank to test degraded-research path
```

The defaults (`AURA_LLM_MODEL=openai/gpt-4o-mini`, `AURA_IMAGE_MODEL=google/gemini-2.5-flash-image-preview`) are fine. The image stage uses OpenRouter's chat-completions endpoint with `modalities=["image","text"]` since OpenRouter does not expose `/images/generations`.

### 1.3. Start the supporting services (ChromaDB + MLflow)

```powershell
docker compose up -d chromadb mlflow
```

Wait ~5 seconds for them to be ready. Sanity check:

```powershell
docker ps --filter "name=aura-"
```

You should see `aura-chromadb` (port 8001→8000) and `aura-mlflow` (port 5000) in `Up` state.

> **If you previously ran the stack and hit a name conflict**: `docker rm -f aura-chromadb aura-mlflow` then retry.

### 1.4. Apply the database migration

```powershell
alembic -c backend/persistence/alembic.ini upgrade head
```

This creates `backend/data/aura.db` with the schema from [data-model.md](../specs/001-aura-marketing-platform/data-model.md).

### 1.5. Seed the deterministic test brand

```powershell
python -m backend.scripts.seed_test_brand
```

Expected:

```
created brand: id=01HX0000TESTBRAND0000000001 display_name='Aura Test Brand'
```

Re-running prints `brand already present: ...` (idempotent).

---

## 2. Start the API

In a dedicated terminal:

```powershell
uvicorn backend.main:app --reload --port 8000
```

Watch the startup logs for:

- `interrupt sweeper completed` (FR-025 — sweeps any non-terminal runs left over from a prior crash)
- `CampaignRunner started` (background consumer is alive)
- `Application startup complete.`

---

## 3. Health check

```powershell
curl.exe -s http://localhost:8000/api/v1/healthz | ConvertFrom-Json
```

Expected:

```json
{
  "status": "ok",
  "components": {
    "database": "ok",
    "chromadb": "ok"
  }
}
```

> **`chromadb: unreachable`?** Your shell hasn't picked up `CHROMA_HOST=localhost` / `CHROMA_PORT=8001`. Either restart uvicorn after editing `.env`, or set them inline: `$env:CHROMA_HOST = "localhost"; $env:CHROMA_PORT = "8001"`.

---

## 4. Happy path — submit a brief

> **PowerShell + `curl` gotcha**: the built-in `curl` alias points to `Invoke-WebRequest`, which mangles JSON bodies. Always use `curl.exe` explicitly, or use `Invoke-RestMethod`.

### 4.1. Submit (option A — `curl.exe`)

> **PowerShell gotcha**: `ConvertTo-Json` pretty-prints by default, and the embedded newlines/braces interact badly with how PowerShell tokenizes args for native exes — FastAPI ends up reading an effectively empty body. Use `-Compress` and pipe the JSON via stdin (`-d '@-'`) to dodge it entirely.

```powershell
$body = @{
    brief = "Launch our new lightweight summer running shoe — the Aura Breeze. Highlight breathability and comfort."
    platform = "instagram"
    brand_id = "01HX0000TESTBRAND0000000001"
    target_audience = "18-34 urban runners, fitness-curious"
} | ConvertTo-Json -Compress

$body | curl.exe -s -X POST http://localhost:8000/api/v1/campaigns `
  -H "Content-Type: application/json" -d '@-'
```

### 4.1. Submit (option B — `Invoke-RestMethod`, recommended)

```powershell
$resp = Invoke-RestMethod -Method POST -Uri http://localhost:8000/api/v1/campaigns `
  -ContentType "application/json" `
  -Body (@{
      brief = "Launch our new lightweight summer running shoe — the Aura Breeze. Highlight breathability and comfort."
      platform = "instagram"
      brand_id = "01HX0000TESTBRAND0000000001"
      target_audience = "18-34 urban runners, fitness-curious"
  } | ConvertTo-Json)

$resp
$runId = $resp.run_id
```

Expected (HTTP 202, returned in **< 1s** per SC-002):

```json
{
  "run_id": "01HX...",
  "status": "queued"
}
```

Save the `run_id` — you'll poll it next.

### 4.2. Poll until terminal

```powershell
do {
    Start-Sleep -Milliseconds 500
    $run = Invoke-RestMethod "http://localhost:8000/api/v1/campaigns/$runId"
    "{0,-8} progress={1:P0} stage={2}" -f $run.status, $run.progress, $run.current_stage
} while ($run.status -notin @("done","failed"))

$run | ConvertTo-Json -Depth 6
```

Expected progression (each line ~500ms apart):

```
queued   progress=0 %  stage=
running  progress=10%  stage=research
running  progress=20%  stage=retrieval
running  progress=50%  stage=copy
running  progress=75%  stage=image
running  progress=90%  stage=critic
done     progress=100% stage=
```

Total wall-clock: target **< 60s** (SC-001). With `google/gemini-2.5-flash-image-preview` the image stage typically runs 4-10s; if you switch to `openai/gpt-image-1` expect 15-30s in `image`.

### 4.3. Validate the response shape

The final `$run` object should contain:

- `status: "done"`
- `output.ad_copy` — `{ headline, primary_text, cta, platform: "instagram" }`, all non-empty
- `output.image_url` — matches `^/api/v1/artifacts/[A-Za-z0-9]+/[A-Za-z0-9]+\.png$`
- `output.score.breakdown` — contains all four required keys: `relevance`, `brand_fit`, `clarity`, `factual_alignment`, each in `[0.0, 1.0]`
- `output.score.passed` — `true` if `overall >= 0.7` (default threshold)
- `trace` — exactly 5 entries in order: `research`, `retrieval`, `copy`, `image`, `critic`
- `trace[*].model_calls[*].provider` — `"openrouter"` for copy/image/critic; `"tavily"` for research (or status `"degraded"` if no Tavily key)

### 4.4. View the generated image

```powershell
Start-Process "http://localhost:8000$($run.output.image_url)"
```

Opens the PNG in your default browser. The file lives on disk under `backend/data/artifacts/{brand_id}/{run_id}.png`.

---

## 5. List runs

```powershell
Invoke-RestMethod "http://localhost:8000/api/v1/campaigns?brand_id=01HX0000TESTBRAND0000000001&limit=10" `
  | ConvertTo-Json -Depth 4
```

Returns `RunSummary[]` (id, brand_id, platform, status, started_at, completed_at).

---

## 6. Streamlit UI (optional, end-user view)

In another terminal:

```powershell
$env:AURA_API_BASE = "http://localhost:8000"
streamlit run frontend/streamlit_app.py
```

Open http://localhost:8501 → Generate Campaign → fill in the form (use the same `brand_id`) → Submit. The Results screen polls the API and renders the image inline once the run reaches `done`.

---

## 7. Failure path — FR-021 hard-fail (SC-005 30s budget)

Verifies that an upstream provider failure surfaces within 30 seconds with `status='failed'` and **no** `output`.

### 7.1. Stop uvicorn (Ctrl+C in its terminal).

### 7.2. Run *without* the OpenRouter key:

```powershell
$env:OPENROUTER_API_KEY = ""
uvicorn backend.main:app --port 8000
```

### 7.3. Submit any brief (reuse the body from §4.1) and poll:

```powershell
$resp = Invoke-RestMethod -Method POST -Uri http://localhost:8000/api/v1/campaigns `
  -ContentType "application/json" `
  -Body (@{
      brief = "test"; platform = "instagram"
      brand_id = "01HX0000TESTBRAND0000000001"; target_audience = "ya"
  } | ConvertTo-Json)

$started = Get-Date
do {
    Start-Sleep -Milliseconds 200
    $run = Invoke-RestMethod "http://localhost:8000/api/v1/campaigns/$($resp.run_id)"
} while ($run.status -notin @("done","failed"))
"failed in {0:N1}s — stage={1} reason={2}" -f ((Get-Date) - $started).TotalSeconds, $run.failed_stage, $run.failed_reason
```

Expected:

- Elapsed time **< 30s** (SC-005)
- `$run.status` → `"failed"`
- `$run.failed_stage` → `"copy"` (the first OpenRouter-backed stage)
- `$run.failed_reason` → contains `"openrouter"` and `"OPENROUTER_API_KEY not set"`
- `$run.output` → `$null`

Stop uvicorn, restore the key in `.env`, and restart for normal use.

---

## 8. Degraded-research path (FR-021 degradable)

Verifies that a missing/failing third-party (Tavily) keeps the run alive.

1. In `.env`, leave `TAVILY_API_KEY=` blank (or set to an obviously bad value).
2. Restart uvicorn.
3. Submit a brief and poll as in §4.

Expected: run reaches `status='done'` (not failed), and `trace[0]` (the research entry) has:

- `status: "degraded"`
- `error_message`: non-null, mentions missing key or HTTP error
- `model_calls`: empty array

---

## 9. Concurrency cap (SC-008)

Default cap is `AURA_CONCURRENCY_CAP=5`. To see queueing behavior, drop it:

```powershell
# stop uvicorn first
$env:AURA_CONCURRENCY_CAP = "1"
uvicorn backend.main:app --port 8000
```

In a second terminal, fire 3 submissions back-to-back:

```powershell
1..3 | ForEach-Object {
    $r = Invoke-RestMethod -Method POST -Uri http://localhost:8000/api/v1/campaigns `
      -ContentType "application/json" `
      -Body (@{
          brief = "burst $_"; platform = "instagram"
          brand_id = "01HX0000TESTBRAND0000000001"; target_audience = "ya"
      } | ConvertTo-Json)
    "$($r.run_id) → $($r.status)"
}
```

All three return HTTP 202 with `status='queued'` in **< 1s** each (SC-002). They then drain serially through `running → done` because the cap is 1.

---

## 10. MLflow trace (FR-019 observability)

Open http://localhost:5000 → experiment `aura` → pick the latest run. You should see:

- **Params**: `brand_id`, `platform`, `brief_chars`, `attempt_count`, `retry_cap`, `critic_threshold`
- **Metrics**: per-stage durations (`research_ms`, `retrieval_ms`, `copy_ms`, `image_ms`, `critic_ms`), critic dimension scores
- **Artifacts**: `final_image.png`, `ad_copy.json`, `trace.json`

If MLflow is down, the API still works — the tracker swallows its own failures.

---

## 11. Cleanup

```powershell
docker compose down                       # stops chromadb + mlflow (keeps volumes)
docker compose down -v                    # also wipes Chroma + MLflow data
Remove-Item backend/data/aura.db          # reset SQLite (re-run alembic + seed)
Remove-Item -Recurse backend/data/artifacts/*   # purge generated images
```

---

## What's covered by this test

| Success criterion | Verified in |
|---|---|
| **SC-001** — single brief done in ≤ 60s p50 | §4.2 (wall-clock observation) |
| **SC-002** — submit returns ≤ 1s p95 | §4.1, §9 |
| **SC-005** — failure surfaces ≤ 30s | §7 |
| **SC-008** — 5 concurrent runs without crashing | §9 (set cap=1 to observe queuing) |
| **SC-009** — per-stage trace observable | §4.3 (`trace[]` in response) + §10 (MLflow) |
| **FR-009** — research∥retrieval parallel fan-out | §4.2 (both stages tick before copy) |
| **FR-021** hard-fail | §7 |
| **FR-021** degradable | §8 |
| **FR-023** image as filesystem artifact + URL | §4.4 |
| **FR-025** restart sweeper | §2 (startup log) |
