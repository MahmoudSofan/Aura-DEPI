"""Contract tests for the brands API per `contracts/openapi.yaml` (T037)."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_post_brands_201_with_minted_id(api_client: TestClient) -> None:
    resp = api_client.post("/api/v1/brands", json={"display_name": "ACME Footwear"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert isinstance(body["id"], str) and body["id"]
    assert body["display_name"] == "ACME Footwear"
    assert "created_at" in body
    assert "updated_at" in body


def test_post_brands_422_on_missing_display_name(api_client: TestClient) -> None:
    resp = api_client.post("/api/v1/brands", json={})
    assert resp.status_code == 422


def test_post_brands_422_on_blank_display_name(api_client: TestClient) -> None:
    resp = api_client.post("/api/v1/brands", json={"display_name": ""})
    assert resp.status_code == 422


def test_post_brands_422_on_extra_field(api_client: TestClient) -> None:
    resp = api_client.post(
        "/api/v1/brands",
        json={"display_name": "X", "id": "should-not-be-allowed"},
    )
    assert resp.status_code == 422


def test_get_brands_returns_list_newest_first(api_client: TestClient) -> None:
    a = api_client.post("/api/v1/brands", json={"display_name": "First"})
    b = api_client.post("/api/v1/brands", json={"display_name": "Second"})
    assert a.status_code == 201
    assert b.status_code == 201

    listing = api_client.get("/api/v1/brands")
    assert listing.status_code == 200
    body = listing.json()
    assert isinstance(body, list) and len(body) >= 2
    # Newest first (created_at descending).
    assert body[0]["created_at"] >= body[1]["created_at"]


def test_get_brand_by_id(api_client: TestClient) -> None:
    created = api_client.post("/api/v1/brands", json={"display_name": "Lookup Brand"}).json()
    bid = created["id"]

    resp = api_client.get(f"/api/v1/brands/{bid}")
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Lookup Brand"


def test_get_brand_404_unknown(api_client: TestClient) -> None:
    resp = api_client.get("/api/v1/brands/01HXNOSUCHBRAND00000000000")
    assert resp.status_code == 404


def test_delete_brand_204(api_client: TestClient) -> None:
    created = api_client.post("/api/v1/brands", json={"display_name": "To Delete"}).json()
    bid = created["id"]

    resp = api_client.delete(f"/api/v1/brands/{bid}")
    assert resp.status_code == 204

    follow_up = api_client.get(f"/api/v1/brands/{bid}")
    assert follow_up.status_code == 404


def test_delete_brand_404_unknown(api_client: TestClient) -> None:
    resp = api_client.delete("/api/v1/brands/01HXNOSUCHBRAND00000000000")
    assert resp.status_code == 404
