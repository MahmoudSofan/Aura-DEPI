# Aura API Contract

FastAPI service exposing campaign generation. Base URL: `/api/v1`.

All request/response bodies are JSON. Schemas mirror [`agents/schemas.py`](../agents/schemas.py).

---

## 1. `POST /campaigns`

Submit a new campaign generation job. Returns immediately with a `run_id`; generation runs asynchronously.

**Request body** — `CampaignRequest`:

```json
{
  "brief": "Launch summer sneaker line targeting Gen Z runners",
  "platform": "instagram",
  "brand_id": "brand_acme_001",
  "target_audience": "18-24, urban, fitness-curious"
}
```

| Field             | Type                                                                                | Required | Notes                |
| ----------------- | ----------------------------------------------------------------------------------- | -------- | -------------------- |
| `brief`           | string                                                                              | yes      | non-empty            |
| `platform`        | enum: `facebook` \| `instagram` \| `tiktok` \| `twitter` \| `linkedin` \| `youtube` | yes      |                      |
| `brand_id`        | string                                                                              | yes      | resolves brand voice |
| `target_audience` | string                                                                              | yes      | non-empty            |

**Response `202 Accepted`**:

```json
{
  "run_id": "9f3c1a2e-...",
  "status": "pending"
}
```

**Errors**: `400` invalid body, `404` unknown `brand_id`, `422` schema validation.

---

## 2. `GET /campaigns/{run_id}`

Poll for status and (when complete) the full campaign artifact.

**Path params**: `run_id` — string returned by `POST /campaigns`.

**Response `200 OK`** — discriminated by `status`:

**Pending / running**:

```json
{ "run_id": "9f3c...", "status": "running" }
```

**Completed** — body is `Campaign`:

```json
{
  "run_id": "9f3c...",
  "status": "completed",
  "campaign": {
    "request": { "brief": "...", "platform": "instagram", "brand_id": "...", "target_audience": "..." },
    "ad_copy": {
      "headline": "Run the city.",
      "primary_text": "Built for the streets you actually run.",
      "cta": "Shop now",
      "platform": "instagram"
    },
    "image": {
      "path": "s3://aura/runs/9f3c.../hero.png",
      "prompt": "...",
      "negative_prompt": "...",
      "dimensions": [1080, 1080]
    },
    "score": {
      "overall": 0.84,
      "breakdown": { "relevance": 0.9, "brand_fit": 0.8, "clarity": 0.82 },
      "feedback": "Strong CTA, slightly generic visual.",
      "passed": true
    }
  }
}
```

**Failed**:

```json
{ "run_id": "9f3c...", "status": "failed", "error": "research_agent: tavily timeout" }
```

**Errors**: `404` unknown `run_id`.

---

## 3. `GET /healthz`

Liveness probe. No auth.

**Response `200 OK`**:

```json
{ "status": "ok" }
```

Returns `503` if downstream dependencies (ChromaDB, MLflow) are unreachable.

---

## Conventions

- **Status enum**: `pending` → `running` → `completed` | `failed`.
- **IDs**: `run_id` is a UUIDv4 string.
- **Errors**: standard FastAPI shape — `{ "detail": "..." }`.
- **Auth**: TBD (likely bearer token in `Authorization` header).
