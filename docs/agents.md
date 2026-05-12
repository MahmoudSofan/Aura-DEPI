# Agents

Aura's pipeline is a LangGraph state machine with **five stage nodes**:
research, retrieval, copy, image, and critic. Each stage is a single
`run_*` function under [`agents/stages/`](../agents/stages/) that takes
its typed Pydantic inputs and returns its typed Pydantic output plus a
`list[ModelCall]` describing the external calls it made. The graph is
defined in [`agents/graph.py`](../agents/graph.py); the runner that
drives it is [`backend/orchestrator/runner.py`](../backend/orchestrator/runner.py).

## Graph topology

```text
START ──┬─► research ──┐
        │              ├─► copy ─► image ─► critic ──┬─► END   (pass / cap reached)
        └─► retrieval ─┘     ▲                       │
                             └───────────────────────┘ (retry with feedback)
```

Per FR-009, `research` and `retrieval` execute **in parallel** on attempt
1; later attempts reuse their outputs (the brand corpus does not change
mid-run, and re-querying Tavily on every retry would waste latency).
`copy`, `image`, and `critic` run **sequentially per attempt**. The
conditional edge after `critic` routes back to `copy` if `will_retry` is
true and to `END` otherwise.

`will_retry` is true iff the verdict failed AND `attempt < retry_cap + 1`.
With the default `AURA_RETRY_CAP=2`, a run does up to three creative+
image+critic attempts before delivering the highest-scoring one as the
winner (FR-015, FR-016).

## Stages

### 1. Research — `agents/stages/research.py`

**Inputs**: `CampaignRequest`.
**Outputs**: `ResearchOutput { trends, competitors, sources }`.
**External**: Tavily Search API (`tavily-python`).
**Failure class**: **degradable** (FR-021).

The only degradable stage. On timeout, missing `TAVILY_API_KEY`, or any
Tavily error, the stage returns an empty `ResearchOutput` plus
`status='degraded'` and a human-readable reason — it never raises. The
critic is informed when research is empty so it doesn't penalize missing
market context (see [agents/stages/critic.py:48-53](../agents/stages/critic.py#L48-L53)).

Default per-call deadline: 10 seconds. Max results: 5.

### 2. Retrieval — `agents/stages/retrieval.py`

**Inputs**: `CampaignRequest`, `chroma_client`.
**Outputs**: `RetrievedContext { chunks: list[Chunk], brand_voice: str }`.
**External**: ChromaDB (local sentence-transformers `all-MiniLM-L6-v2` embedding, in-process — counted as `provider='huggingface'` in `ModelCall`).
**Failure class**: **hard-fail**.

Resolves `brand_id` to the per-brand Chroma collection name
`brand_{brand_id}` and runs a top-K similarity query against it.
**Brand isolation (FR-005) is enforced structurally**: there is no API
path that queries a different brand's collection, so a missing
`where`-clause cannot leak cross-brand data — the structural per-brand
collection is the discipline.

If the brand has zero ingested documents, the collection is empty,
`chunks=[]` and `brand_voice=""` — the campaign still runs (research-
only-grounded), and the critic is told the same way it is for degraded
research (see spec Edge Cases).

### 3. Copy — `agents/stages/copy.py`

**Inputs**: `CampaignRequest`, `ResearchOutput`, `RetrievedContext`, `prior_critic_feedback: str | None`.
**Outputs**: `AdCopy { headline, primary_text, cta, platform }`.
**External**: OpenRouter chat completions (default `openai/gpt-4o-mini`, env `AURA_LLM_MODEL`).
**Failure class**: **hard-fail**.

The system prompt embeds:

- the platform's length budget (see [`research.md §12`](../specs/001-aura-marketing-platform/research.md) for the per-platform character table — `facebook` 40/125/20, `linkedin` 60/700/25, etc.);
- up to five retrieved chunks plus the brand voice phrase;
- the research summary if present;
- `prior_critic_feedback` from the previous attempt if this is a retry — that's how the FR-015 retry loop carries forward (the critic's written feedback informs the next attempt).

Returns structured JSON validated against the `AdCopy` schema. Malformed
output is a `StageError` (hard-fail).

### 4. Image — `agents/stages/image.py`

**Inputs**: `AdCopy`, `CampaignRequest`, `output_path: Path`, `prior_critic_feedback`, `image_url: str` (the canonical `/api/v1/artifacts/{brand_id}/{run_id}.png`).
**Outputs**: `GeneratedImage { path, prompt, negative_prompt, dimensions }`.
**External**: OpenRouter chat completions with `modalities=["image","text"]` (default `google/gemini-2.5-flash-image-preview`, env `AURA_IMAGE_MODEL`).
**Failure class**: **hard-fail**.

Note: OpenRouter does not expose `/images/generations`, so image
generation rides on the chat-completions endpoint with `modalities`.
Bytes are written to `output_path` (the per-attempt PNG); the runner
later promotes the winning attempt's file to the canonical
`{run_id}.png` and deletes the losing attempts (see
[runner.py:329](../backend/orchestrator/runner.py#L329)).

The `path` field on the returned `GeneratedImage` is the canonical
**API-relative URL**, never an absolute filesystem path and never inline
base64 (FR-023). Bytes are served by `GET /api/v1/artifacts/{brand_id}/{filename}`.

### 5. Critic — `agents/stages/critic.py`

**Inputs**: `AdCopy`, `GeneratedImage`, `RetrievedContext`, `CampaignRequest`, `threshold: float`.
**Outputs**: `CriticScore { overall, breakdown, feedback, passed }`.
**External**: OpenRouter chat completions (same model as copy by default).
**Failure class**: **hard-fail**.

Returns a structured-output verdict with four required dimensions:

- `relevance` — does the copy address the brief and audience?
- `brand_fit` — does the copy reflect the brand's tone/positioning per the retrieved chunks?
- `clarity` — is the copy well-structured, platform-appropriate, free of unsupported claims?
- `factual_alignment` — do specific claims in the copy trace to retrieved chunks?

`overall` is the equal-weighted mean of the four dimensions. `passed =
overall >= AURA_CRITIC_THRESHOLD` (default 0.7). `feedback` is the
free-text rationale that gets fed back into the next attempt's copy and
image prompts when `passed=False`.

Critic dimensions are also what indirectly enforce FR-011 (platform tone)
and FR-013 (image relevance) — there is no separate length validator or
image-quality validator. Persistent tone or image drift surfaces as low
scores on `clarity` / `relevance` / `brand_fit` and triggers the retry
loop.

## Data flow types

All inter-stage payloads are Pydantic v2 models in
[`agents/schemas.py`](../agents/schemas.py), all with
`model_config = ConfigDict(extra="forbid")`. The end-to-end flow:

```text
CampaignRequest ──► research ──► ResearchOutput ──┐
                ──► retrieval ──► RetrievedContext─┴──► copy ──► AdCopy ──► image ──► GeneratedImage
                                                                                          │
                                                                                          ▼
                                                                                       critic
                                                                                          │
                                                                                          ▼
                                                                                     CriticScore
                                                                                          │
                                                                                          ▼
                                                                       Campaign { request, ad_copy, image, score, run_id }
```

## Per-stage trace

Every stage emits a `StageTraceEntry` to the runner's `on_stage_event`
callback ([graph.py:107](../agents/graph.py#L107)). The runner persists
each entry as a row in the `stage_traces` SQLite table inside its own
transaction, so partial state is never visible. Each row carries:

- `stage`, `attempt`, `status` (`ok` | `degraded` | `failed`)
- `started_at`, `completed_at`, `duration_ms`
- `model_calls` — `[{provider, model, op, latency_ms, token_in, token_out}]`
- `verdict` — only set for `stage='critic'`
- `error_message` — set when `status` is `degraded` or `failed`

That's the audit trail surfaced via `GET /api/v1/campaigns/{run_id}.trace`
(FR-019, SC-009). The same data also feeds MLflow — see
[evaluation.md](evaluation.md).

## Winner selection (FR-016)

The graph records every attempt's `(ad_copy, image, image_path, critic)`
in `GraphState.attempts`. After the graph terminates the runner picks
the highest-scoring attempt with `max(attempts, key=lambda a: a.critic.overall)`
([runner.py:213](../backend/orchestrator/runner.py#L213)). That attempt's
PNG is promoted to the canonical `{run_id}.png` path; the losing
attempts' PNGs are deleted. `campaign_outputs.winning_attempt` records
which attempt was picked so the audit trace remains coherent.

The run is marked `done` even when no attempt passed the threshold — the
final score and `passed=False` flag are recorded honestly. This is the
FR-016 contract: deliver the best of what we got, never a "failed" with
no output unless an actual stage hard-failed.

## Implementation notes

- Stage functions are **synchronous**; `agents/graph.py` wraps each call in `asyncio.to_thread` so the event loop is never blocked on OpenAI HTTP, ChromaDB queries, or PNG bytes.
- Stage `StageError` exceptions are decorated with a `stage` attribute by `agents.graph._stage_error` ([graph.py:120](../agents/graph.py#L120)) so the runner can populate `failed_stage` / `failed_reason` accurately.
- Tests stub the external clients (`openai`, `tavily`, `chromadb`) at the module-level `_make_*_client` factories — see [testing.md](testing.md).
