# Evaluation

How Aura measures its own behaviour, both at runtime (the critic agent +
MLflow tracking) and offline (smoke benchmarks under `eval/`). Mapped to
the spec's success criteria (SC-001..SC-010) at the end of the page.

## Two observability sinks

Per [`research.md §10`](../specs/001-aura-marketing-platform/research.md),
stage-level observability has two **independent** sinks:

| Sink | Consumer | What it carries | Failure mode |
|------|----------|-----------------|--------------|
| SQLite `stage_traces` table | Operators via `GET /api/v1/campaigns/{run_id}.trace` | inputs/outputs (compact), duration_ms, model_calls, optional critic verdict, optional error | authoritative for FR-019; never depends on MLflow being up |
| MLflow experiment `aura` | Developers / evaluators via `http://localhost:5000` | params, per-stage durations, critic dimension scores, artifacts (trace.json, ad_copy.json, final_image.png) | MLflow downtime never breaks an Aura run — the logger swallows exceptions ([stage_tracking.py:106](../eval/stage_tracking.py#L106)) |

The orchestrator writes to the SQLite trace **as each stage completes**;
MLflow is written **once, at terminal**, with the full run snapshot.
That batching keeps MLflow latency off the run's wall-clock budget
(SC-001).

## MLflow run shape

Each Aura run produces one MLflow run named `aura-{run_id}`, tagged
`aura_run_id` and `aura_brand_id`, with:

### Params
- `brand_id`, `platform`, `brief_chars`
- `attempt_count`, `retry_cap`, `critic_threshold`
- `status` (`done` or `failed`)

### Metrics
- `duration_research_ms`, `duration_retrieval_ms`, `duration_copy_ms`, `duration_image_ms`, `duration_critic_ms` — summed across attempts
- `total_duration_ms`
- `critic_overall` — winning attempt's overall score
- `critic_relevance`, `critic_brand_fit`, `critic_clarity`, `critic_factual_alignment` — per-dimension scores from the winning verdict

### Artifacts
- `trace.json` — every `StageTraceEntry` for the run (all attempts)
- `ad_copy.json` — the delivered `AdCopy` (absent for `failed` runs)
- `final_image.png` — the winning attempt's PNG (absent for `failed` runs or when the file is missing)

Code: [`eval/stage_tracking.py`](../eval/stage_tracking.py) +
[`eval/tracking.py`](../eval/tracking.py). Tracking URI and experiment
come from `MLFLOW_TRACKING_URI` (default `http://localhost:5000`) and
`MLFLOW_EXPERIMENT` (default `aura`).

## Critic dimensions

The critic is **the** quality gate. Four required dimensions (validated
in [`agents/stages/critic.py:30-35`](../agents/stages/critic.py#L30)):

| Dimension | What it scores |
|-----------|---------------|
| `relevance` | Does the copy address the brief and target audience? |
| `brand_fit` | Does the copy reflect the brand's tone/positioning per the retrieved chunks? |
| `clarity` | Is the copy well-structured, platform-appropriate, and free of unsupported claims? |
| `factual_alignment` | Do specific claims in the copy trace back to retrieved chunks? |

`overall = mean(four dimensions)` with equal weights. `passed = overall
>= AURA_CRITIC_THRESHOLD` (default 0.7). The `breakdown` is an open dict
(`dict[str, float]`) so additional dimensions can be added without a
schema break.

The critic indirectly enforces:

- **FR-011** (platform tone/length) — surfaces as low `clarity`.
- **FR-013** (image relevance) — surfaces as low `relevance` / `brand_fit`.

When research is degraded, the critic prompt is told to not penalise the
missing market context.

## Smoke benchmarks

Under [`eval/benchmarks/`](../eval/benchmarks/). Each is a standalone
script that builds a fully-stubbed stack (no external API calls), drives
the API end-to-end, and prints a one-line verdict. They run fast
(seconds) and are deterministic by design — they verify behaviour
guarantees, not absolute LLM quality.

### `e2e_latency_smoke.py` — SC-001 wall-clock

Submits a brief through the public API with stubbed copy/critic/image
responses configured to pass on attempt 1, then asserts the end-to-end
duration is within budget. Useful to catch regressions in the
orchestrator / graph plumbing.

### `concurrency_smoke.py` — SC-008 concurrency cap

Submits N briefs in quick succession with `AURA_CONCURRENCY_CAP=5` (or
whatever is configured) and asserts:

- the submission API returns `202` for every brief, including those that
  end up queued;
- no executing run's duration exceeds `1.5×` its solo baseline;
- queue ordering is FIFO.

### `repeatability_smoke.py` — SC-007 quality stability

Submits the same brief five times against the same brand with the same
stubbed responses, then asserts the final `critic_overall` scores fall
within ±0.1 of each other.

### `brand_grounding_smoke.py` — SC-003 / SC-004 grounding + isolation

Uploads brand-distinctive documents to two brands, runs campaigns for
each, and asserts:

- the delivered copy for Brand A references at least one fact from
  Brand A's document (SC-003);
- the retrieval-stage trace for Brand A's run contains **only** chunks
  from Brand A's documents — never Brand B's (SC-004).

Run a benchmark directly:

```powershell
python -m eval.benchmarks.e2e_latency_smoke
python -m eval.benchmarks.concurrency_smoke
python -m eval.benchmarks.repeatability_smoke
python -m eval.benchmarks.brand_grounding_smoke
```

Each prints `OK: ...` on success, or raises with a diagnostic on
failure. The shared harness is in
[`eval/benchmarks/_harness.py`](../eval/benchmarks/_harness.py); see
[testing.md](testing.md) for how stubs are wired.

## Live-mode smoke

Most tests in `eval/tests/` and the benchmarks above run with stubbed
LLM/Tavily/image clients. To exercise the real OpenRouter / Tavily /
ChromaDB path:

```powershell
$env:AURA_RUN_LIVE_TESTS = "1"
pytest -m live
```

This is gated to keep CI deterministic and to avoid burning OpenRouter
credits on every push. See [testing.md#live-tests](testing.md#live-tests).

## Mapping to spec success criteria

| Criterion | What it claims | How it's verified |
|-----------|----------------|------------------|
| **SC-001** | < 60s p50 end-to-end | `eval/benchmarks/e2e_latency_smoke.py`; live with real models for true p50 |
| **SC-002** | < 1s p95 submit ack | submission API returns after one SQL insert + queue push — verifiable via timing in `concurrency_smoke.py` |
| **SC-003** | ≥80% of campaigns reference brand-document facts | `brand_grounding_smoke.py` for the structural check; subjective verification needs human labels |
| **SC-004** | 100% brand isolation across 100 paired runs | `brand_grounding_smoke.py` asserts retrieval-stage trace; reinforced structurally by per-brand Chroma collections |
| **SC-005** | ≤30s to surface stage failure/degradation | Tavily 10s timeout + per-stage hard-fail behaviour; quickstart §7.1/7.2 |
| **SC-006** | Completed runs survive restart | FR-020 — every state transition is committed before downstream actions; restart sweep handles in-flight only |
| **SC-007** | ±0.1 stability across 5 repeats | `repeatability_smoke.py` |
| **SC-008** | 5 concurrent runs within 1.5× solo baseline | `concurrency_smoke.py` |
| **SC-009** | Per-stage trace explains every run | `stage_traces` table + MLflow `trace.json` artifact |
| **SC-010** | ≤5 min operator clock for the happy path | quickstart walked end-to-end |

## Where to look for evaluation artefacts

| What | Where |
|------|-------|
| Per-run audit trace | `GET /api/v1/campaigns/{run_id}` response, `trace` field |
| MLflow experiment | `http://localhost:5000` → `aura` experiment |
| Generated PNGs | `backend/data/artifacts/{brand_id}/{run_id}.png` and `GET /api/v1/artifacts/{brand_id}/{run_id}.png` |
| Raw uploaded documents | `backend/data/uploads/{brand_id}/{document_id}.{ext}` (retained, including parse-rejected uploads) |
| Smoke benchmark results | stdout of `python -m eval.benchmarks.<name>` |
