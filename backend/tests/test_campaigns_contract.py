"""Contract tests for the campaigns API per `contracts/openapi.yaml`."""

from __future__ import annotations

from fastapi.testclient import TestClient

VALID_BRIEF = {
    "brief": "Launch summer sneaker line. Lightweight mesh, free shipping over $75.",
    "platform": "instagram",
    "brand_id": "01HXTESTBRAND0000000000000",
    "target_audience": "18-24 urban fitness-curious",
}


def test_post_campaigns_returns_202_with_run_id(
    api_client: TestClient, seeded_brand_id_via_client: str
) -> None:
    body = {**VALID_BRIEF, "brand_id": seeded_brand_id_via_client}
    resp = api_client.post("/api/v1/campaigns", json=body)
    assert resp.status_code == 202, resp.text
    payload = resp.json()
    assert payload["status"] == "queued"
    assert isinstance(payload["run_id"], str) and payload["run_id"]


def test_post_campaigns_404_on_unknown_brand(api_client: TestClient) -> None:
    body = {**VALID_BRIEF, "brand_id": "01HXNOSUCHBRAND00000000000"}
    resp = api_client.post("/api/v1/campaigns", json=body)
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


def test_post_campaigns_422_on_unknown_platform(
    api_client: TestClient, seeded_brand_id_via_client: str
) -> None:
    body = {**VALID_BRIEF, "brand_id": seeded_brand_id_via_client, "platform": "myspace"}
    resp = api_client.post("/api/v1/campaigns", json=body)
    assert resp.status_code == 422


def test_post_campaigns_422_on_missing_field(
    api_client: TestClient, seeded_brand_id_via_client: str
) -> None:
    body = {
        "platform": "instagram",
        "brand_id": seeded_brand_id_via_client,
        "target_audience": "ya",
    }
    resp = api_client.post("/api/v1/campaigns", json=body)
    assert resp.status_code == 422


def test_get_campaign_404_on_unknown_run_id(api_client: TestClient) -> None:
    resp = api_client.get("/api/v1/campaigns/01HXNOSUCHRUN00000000000000")
    assert resp.status_code == 404


def test_list_campaigns_filters(
    api_client: TestClient,
    seeded_brand_id_via_client: str,
    stub_openai: object,
    stub_image: object,
) -> None:
    """``GET /api/v1/campaigns?brand_id=...&status=...&limit=...`` shape."""

    resp = api_client.get(
        "/api/v1/campaigns",
        params={"brand_id": seeded_brand_id_via_client, "status": "queued", "limit": 10},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)


def test_list_campaigns_bad_limit_422(api_client: TestClient) -> None:
    resp = api_client.get("/api/v1/campaigns", params={"limit": 9999})
    assert resp.status_code == 422
