from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from relocarr.config import settings as app_settings
from relocarr.db import init_db, session


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "config_dir", tmp_path)
    init_db(app_settings.db_path)
    from relocarr.main import app

    return TestClient(app)


def _insert_app(staging_root: str, kind: str = "radarr") -> int:
    with session(app_settings.db_path) as conn:
        cur = conn.execute(
            """INSERT INTO apps (kind, name, base_url, api_key, webhook_token, staging_root, target_root)
               VALUES (?, 'App', 'http://x', 'key', 'token', ?, '')""",
            (kind, staging_root),
        )
        return cur.lastrowid


def test_app_detail_404_for_unknown_app(client):
    resp = client.get("/settings/apps/999")
    assert resp.status_code == 404


def test_app_detail_shows_error_for_same_path_as_staging(client, tmp_path):
    app_id = _insert_app(str(tmp_path))
    with session(app_settings.db_path) as conn:
        conn.execute("UPDATE apps SET target_root = ? WHERE id = ?", (str(tmp_path), app_id))

    resp = client.get(f"/settings/apps/{app_id}")
    assert resp.status_code == 200
    assert "must not be the same path" in resp.text


def test_app_detail_shows_ok_for_valid_distinct_target(client, tmp_path):
    staging = tmp_path / "staging"
    target = tmp_path / "target"
    staging.mkdir()
    target.mkdir()
    app_id = _insert_app(str(staging))
    with session(app_settings.db_path) as conn:
        conn.execute("UPDATE apps SET target_root = ? WHERE id = ?", (str(target), app_id))

    resp = client.get(f"/settings/apps/{app_id}")
    assert "OK -- exists, writable, distinct from staging" in resp.text


def test_update_app_persists_fields(client, tmp_path):
    staging = tmp_path / "staging"
    target = tmp_path / "target"
    staging.mkdir()
    target.mkdir()
    app_id = _insert_app(str(staging))

    resp = client.post(
        f"/settings/apps/{app_id}",
        data={
            "target_root": str(target),
            "layout_mode": "preserve",
            "max_file_size_gb": "4.5",
            "conflict_policy": "replace",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    with session(app_settings.db_path) as conn:
        app = conn.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()

    assert app["target_root"] == str(target)
    assert app["layout_mode"] == "preserve"
    assert app["conflict_policy"] == "replace"
    assert app["max_file_size_bytes"] == int(4.5 * 1024**3)


def test_update_app_blank_max_size_means_unlimited(client, tmp_path):
    app_id = _insert_app(str(tmp_path))
    client.post(
        f"/settings/apps/{app_id}",
        data={"target_root": "", "layout_mode": "flat", "max_file_size_gb": "", "conflict_policy": "skip"},
    )
    with session(app_settings.db_path) as conn:
        app = conn.execute("SELECT max_file_size_bytes FROM apps WHERE id = ?", (app_id,)).fetchone()
    assert app["max_file_size_bytes"] is None


def test_app_detail_shows_download_client_read_only(client, tmp_path):
    # Adding/deleting a download client now only happens on the dedicated
    # /settings/download-clients page (de-duplicated per user feedback,
    # 2026-10-22 -- App Detail used to have its own separate add/delete
    # form doing the exact same thing). This page just displays it.
    app_id = _insert_app(str(tmp_path))
    with session(app_settings.db_path) as conn:
        conn.execute(
            """INSERT INTO download_clients (app_id, kind, host, port)
               VALUES (?, 'transmission', 'transmission', 9091)""",
            (app_id,),
        )

    resp = client.get(f"/settings/apps/{app_id}")
    assert resp.status_code == 200
    assert "transmission" in resp.text
    assert "9091" in resp.text
    assert "Add Transmission client" not in resp.text  # no leftover add form for the client here
    assert 'href="/settings/download-clients"' in resp.text  # links to the one real place instead


@respx.mock
def test_update_connection_succeeds_and_saves(client, tmp_path):
    app_id = _insert_app(str(tmp_path))
    respx.get("http://new-radarr:7878/api/v3/system/status").mock(
        return_value=httpx.Response(200, json={"version": "6.4.4"})
    )

    resp = client.post(
        f"/settings/apps/{app_id}/connection",
        data={"base_url": "http://new-radarr:7878", "api_key": "newkey"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    with session(app_settings.db_path) as conn:
        app = conn.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
    assert app["base_url"] == "http://new-radarr:7878"
    assert app["api_key"] == "newkey"


def test_update_connection_failure_does_not_save(client, tmp_path):
    app_id = _insert_app(str(tmp_path))

    resp = client.post(
        f"/settings/apps/{app_id}/connection",
        data={"base_url": "http://unreachable-host-xyz:7878", "api_key": "newkey"},
    )
    assert resp.status_code == 200
    assert "Connection failed" in resp.text

    with session(app_settings.db_path) as conn:
        app = conn.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
    assert app["base_url"] == "http://x"  # unchanged -- the bad value was never saved
