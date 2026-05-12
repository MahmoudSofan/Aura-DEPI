"""Contract tests for the documents API per `contracts/openapi.yaml` (T038)."""

from __future__ import annotations

from fastapi.testclient import TestClient


def _create_brand(api_client: TestClient, display_name: str = "Brand") -> str:
    return str(api_client.post("/api/v1/brands", json={"display_name": display_name}).json()["id"])


def test_upload_document_201_happy_path(api_client: TestClient, stub_embeddings: None) -> None:
    bid = _create_brand(api_client, "Docs Brand")
    resp = api_client.post(
        f"/api/v1/brands/{bid}/documents",
        files={"file": ("brief.txt", b"Hello brand world. Free shipping over $75.", "text/plain")},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["brand_id"] == bid
    assert body["original_filename"] == "brief.txt"
    assert body["format"] == "txt"
    assert body["parse_status"] == "parsed"
    assert body["chunk_count"] >= 1
    assert len(body["content_hash"]) == 64


def test_upload_document_404_unknown_brand(api_client: TestClient, stub_embeddings: None) -> None:
    resp = api_client.post(
        "/api/v1/brands/01HXNOSUCHBRAND00000000000/documents",
        files={"file": ("x.txt", b"hello", "text/plain")},
    )
    assert resp.status_code == 404


def test_upload_document_409_duplicate(api_client: TestClient, stub_embeddings: None) -> None:
    bid = _create_brand(api_client, "Dup Brand")
    payload = b"Identical bytes. Brand voice: confident."
    first = api_client.post(
        f"/api/v1/brands/{bid}/documents",
        files={"file": ("a.txt", payload, "text/plain")},
    )
    assert first.status_code == 201
    second = api_client.post(
        f"/api/v1/brands/{bid}/documents",
        files={"file": ("b.txt", payload, "text/plain")},
    )
    assert second.status_code == 409


def test_upload_document_415_unsupported_type(
    api_client: TestClient, stub_embeddings: None
) -> None:
    bid = _create_brand(api_client, "Type Brand")
    resp = api_client.post(
        f"/api/v1/brands/{bid}/documents",
        files={
            "file": (
                "spreadsheet.xlsx",
                b"not really an xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert resp.status_code == 415


def test_upload_document_413_oversize(api_client: TestClient, stub_embeddings: None) -> None:
    bid = _create_brand(api_client, "Big Brand")
    big = b"x" * (52_428_800 + 1)
    resp = api_client.post(
        f"/api/v1/brands/{bid}/documents",
        files={"file": ("huge.txt", big, "text/plain")},
    )
    assert resp.status_code == 413


def test_upload_document_422_unparseable(api_client: TestClient, stub_embeddings: None) -> None:
    """Whitespace-only TXT is treated as unparseable (no usable text)."""

    bid = _create_brand(api_client, "Empty Brand")
    resp = api_client.post(
        f"/api/v1/brands/{bid}/documents",
        files={"file": ("blank.txt", b"   \n\t   ", "text/plain")},
    )
    assert resp.status_code == 422


def test_list_documents_returns_brand_docs_newest_first(
    api_client: TestClient, stub_embeddings: None
) -> None:
    bid = _create_brand(api_client, "List Brand")
    api_client.post(
        f"/api/v1/brands/{bid}/documents",
        files={"file": ("a.txt", b"alpha doc one", "text/plain")},
    )
    api_client.post(
        f"/api/v1/brands/{bid}/documents",
        files={"file": ("b.txt", b"beta doc two -- different", "text/plain")},
    )

    resp = api_client.get(f"/api/v1/brands/{bid}/documents")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    # Newest first.
    assert body[0]["created_at"] >= body[1]["created_at"]


def test_list_documents_404_unknown_brand(api_client: TestClient) -> None:
    resp = api_client.get("/api/v1/brands/01HXNOSUCHBRAND00000000000/documents")
    assert resp.status_code == 404
