import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from relocarr.config import settings as app_settings
from relocarr.db import init_db, session

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "config_dir", tmp_path)
    init_db(app_settings.db_path)

    from relocarr.main import app

    return TestClient(app)


def _insert_app(staging_root: str, target_root: str | None = None) -> tuple[int, str]:
    if target_root is None:
        target_root = staging_root.rstrip("/") + "-target"
        Path(target_root).mkdir(parents=True, exist_ok=True)
    token = "test-token-123"
    with session(app_settings.db_path) as conn:
        cur = conn.execute(
            """INSERT INTO apps (kind, name, base_url, api_key, webhook_token, staging_root, target_root)
               VALUES ('radarr', 'Radarr', 'http://radarr', 'key', ?, ?, ?)""",
            (token, staging_root, target_root),
        )
        return cur.lastrowid, token


def test_webhook_rejects_wrong_token(client, tmp_path):
    app_id, _ = _insert_app(str(tmp_path))
    resp = client.post(
        f"/webhook/{app_id}", json={"eventType": "Test"}, headers={"X-Relocarr-Token": "wrong"}
    )
    assert resp.status_code == 401


def test_webhook_rejects_unknown_app(client):
    resp = client.post(
        "/webhook/99999", json={"eventType": "Test"}, headers={"X-Relocarr-Token": "x"}
    )
    assert resp.status_code == 404


def test_webhook_creates_job_from_real_captured_payload(client, tmp_path):
    payload = json.loads((FIXTURES / "radarr_webhook_download_no_downloadid.json").read_text())

    # The fixture's real source_path is under relocarr's staging tree from
    # the live session; point staging_root at its actual parent so the
    # layout computation resolves correctly.
    staging_root = str(Path(payload["movieFile"]["path"]).parent.parent)
    app_id, token = _insert_app(staging_root)

    resp = client.post(f"/webhook/{app_id}", json=payload, headers={"X-Relocarr-Token": token})

    assert resp.status_code == 200
    assert resp.json() == {"jobs_created": 1}

    with session(app_settings.db_path) as conn:
        job = conn.execute("SELECT * FROM jobs WHERE app_id = ?", (app_id,)).fetchone()
    assert job["state"] == "IMPORTED"
    assert job["media_file_id"] == "19"
    assert job["download_id"] is None
    assert job["target_path"].endswith("Example Film.2002.mp4")
    assert "Example Film2002" not in job["target_path"]  # flat layout, folder dropped


def test_webhook_is_idempotent_for_retried_delivery(client, tmp_path):
    payload = json.loads((FIXTURES / "radarr_webhook_download_no_downloadid.json").read_text())
    staging_root = str(Path(payload["movieFile"]["path"]).parent.parent)
    app_id, token = _insert_app(staging_root)

    first = client.post(f"/webhook/{app_id}", json=payload, headers={"X-Relocarr-Token": token})
    second = client.post(f"/webhook/{app_id}", json=payload, headers={"X-Relocarr-Token": token})

    assert first.json() == {"jobs_created": 1}
    assert second.json() == {"jobs_created": 0}  # de-duplicated by (app_id, media_file_id)

    with session(app_settings.db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE app_id = ?", (app_id,)
        ).fetchone()["n"]
    assert count == 1


def test_webhook_requires_target_root_configured(client, tmp_path):
    app_id, token = _insert_app(str(tmp_path), target_root="")
    payload = {"eventType": "Test"}
    resp = client.post(f"/webhook/{app_id}", json=payload, headers={"X-Relocarr-Token": token})
    assert resp.status_code == 409


def test_webhook_creates_job_when_target_root_is_configured_but_not_mounted(client, tmp_path):
    # Reproduces the real bug hit against the live production deployment,
    # 2026-10-22: a real webhook fired from Radarr 500'd because the
    # destination (target_root) was a configured but currently-unmounted
    # path (the Shield NAS, deliberately off). A job must still get
    # created -- destination availability belongs to the copy step
    # (Section 12's health probe + backoff), not webhook receipt.
    payload = json.loads((FIXTURES / "radarr_webhook_download_no_downloadid.json").read_text())
    staging_root = str(Path(payload["movieFile"]["path"]).parent.parent)
    unmounted_target = str(tmp_path / "mnt" / "nvidia" / "movies")
    assert not Path(unmounted_target).exists()

    app_id, token = _insert_app(staging_root, target_root=unmounted_target)

    resp = client.post(f"/webhook/{app_id}", json=payload, headers={"X-Relocarr-Token": token})

    assert resp.status_code == 200
    assert resp.json() == {"jobs_created": 1}

    with session(app_settings.db_path) as conn:
        job = conn.execute("SELECT * FROM jobs WHERE app_id = ?", (app_id,)).fetchone()
    assert job["state"] == "IMPORTED"
    assert job["target_path"].endswith("Example Film.2002.mp4")
