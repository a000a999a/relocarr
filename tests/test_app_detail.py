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
    resp = client.get("/apps/999")
    assert resp.status_code == 404


def test_app_detail_shows_error_for_same_path_as_staging(client, tmp_path):
    app_id = _insert_app(str(tmp_path))
    with session(app_settings.db_path) as conn:
        conn.execute("UPDATE apps SET target_root = ? WHERE id = ?", (str(tmp_path), app_id))

    resp = client.get(f"/apps/{app_id}")
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

    resp = client.get(f"/apps/{app_id}")
    assert "OK -- exists, writable, distinct from staging" in resp.text


def test_update_app_persists_fields(client, tmp_path):
    staging = tmp_path / "staging"
    target = tmp_path / "target"
    staging.mkdir()
    target.mkdir()
    app_id = _insert_app(str(staging))

    resp = client.post(
        f"/apps/{app_id}",
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
        f"/apps/{app_id}",
        data={"target_root": "", "layout_mode": "flat", "max_file_size_gb": "", "conflict_policy": "skip"},
    )
    with session(app_settings.db_path) as conn:
        app = conn.execute("SELECT max_file_size_bytes FROM apps WHERE id = ?", (app_id,)).fetchone()
    assert app["max_file_size_bytes"] is None


def test_add_download_client(client, tmp_path):
    app_id = _insert_app(str(tmp_path))
    resp = client.post(
        f"/apps/{app_id}/download-client",
        data={
            "host": "transmission",
            "port": "9091",
            "stop_on_completion": "on",
            "remove_after_copy": "on",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    with session(app_settings.db_path) as conn:
        dc = conn.execute("SELECT * FROM download_clients WHERE app_id = ?", (app_id,)).fetchone()

    assert dc["host"] == "transmission"
    assert dc["port"] == 9091
    assert dc["stop_on_completion"] == 1
    assert dc["remove_after_copy"] == 1


def test_delete_download_client_removes_it(client, tmp_path):
    app_id = _insert_app(str(tmp_path))
    with session(app_settings.db_path) as conn:
        dc_id = conn.execute(
            """INSERT INTO download_clients (app_id, kind, host, port)
               VALUES (?, 'transmission', 'transmission', 9091)""",
            (app_id,),
        ).lastrowid

    resp = client.post(f"/apps/{app_id}/download-client/{dc_id}/delete", follow_redirects=False)
    assert resp.status_code == 303

    with session(app_settings.db_path) as conn:
        row = conn.execute("SELECT * FROM download_clients WHERE id = ?", (dc_id,)).fetchone()
    assert row is None


@respx.mock
def test_update_connection_succeeds_and_saves(client, tmp_path):
    app_id = _insert_app(str(tmp_path))
    respx.get("http://new-radarr:7878/api/v3/system/status").mock(
        return_value=httpx.Response(200, json={"version": "6.4.4"})
    )

    resp = client.post(
        f"/apps/{app_id}/connection",
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
        f"/apps/{app_id}/connection",
        data={"base_url": "http://unreachable-host-xyz:7878", "api_key": "newkey"},
    )
    assert resp.status_code == 200
    assert "Connection failed" in resp.text

    with session(app_settings.db_path) as conn:
        app = conn.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
    assert app["base_url"] == "http://x"  # unchanged -- the bad value was never saved
