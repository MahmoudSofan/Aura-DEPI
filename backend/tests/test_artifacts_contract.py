"""Contract tests for the artifact-streaming endpoint."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _seed_artifact(tmp_data_dir: Path, brand_id: str, filename: str) -> Path:
    artifacts_dir = tmp_data_dir / "artifacts" / brand_id
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    target = artifacts_dir / filename
    target.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    return target


def test_get_artifact_streams_png(
    api_client: TestClient,
    seeded_brand_id_via_client: str,
    tmp_data_dir: Path,
) -> None:
    _seed_artifact(tmp_data_dir, seeded_brand_id_via_client, "01HXFAKERUN.png")
    resp = api_client.get(f"/api/v1/artifacts/{seeded_brand_id_via_client}/01HXFAKERUN.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content.startswith(b"\x89PNG"), resp.content[:8]


def test_get_artifact_404_unknown_brand(api_client: TestClient) -> None:
    resp = api_client.get("/api/v1/artifacts/01HXNOSUCHBRAND00000000000/run.png")
    assert resp.status_code == 404


def test_get_artifact_404_missing_file(
    api_client: TestClient, seeded_brand_id_via_client: str
) -> None:
    resp = api_client.get(f"/api/v1/artifacts/{seeded_brand_id_via_client}/01HXFAKERUN.png")
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "filename",
    [
        "../etc/passwd.png",
        "..\\\\windows.png",
        "sub/dir.png",
        "no-extension",
        "evil.exe",
        "file with spaces.png",
    ],
)
def test_get_artifact_rejects_traversal(
    api_client: TestClient, seeded_brand_id_via_client: str, filename: str
) -> None:
    resp = api_client.get(f"/api/v1/artifacts/{seeded_brand_id_via_client}/{filename}")
    assert resp.status_code in (400, 404), resp.status_code
