import pytest
from fastapi.testclient import TestClient

from relocarr.config import settings as app_settings
from relocarr.db import init_db, session


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "config_dir", tmp_path)
    init_db(app_settings.db_path)
    from relocarr.main import app

    return TestClient(app)


def _insert_app(staging_root: str, name: str = "Radarr") -> int:
    with session(app_settings.db_path) as conn:
        cur = conn.execute(
            """INSERT INTO apps (kind, name, base_url, api_key, webhook_token, staging_root, target_root)
               VALUES ('radarr', ?, 'http://x', 'key', 'token', ?, '')""",
            (name, staging_root),
        )
        return cur.lastrowid


def test_root_redirects_to_log(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/log"


def test_settings_redirects_to_apps(client):
    resp = client.get("/settings", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/settings/apps"


def test_app_config_list_shows_apps(client, tmp_path):
    _insert_app(str(tmp_path), name="My Radarr")
    resp = client.get("/settings/apps")
    assert resp.status_code == 200
    assert "My Radarr" in resp.text


def test_app_config_list_empty_state(client):
    resp = client.get("/settings/apps")
    assert resp.status_code == 200
    assert "No apps configured yet" in resp.text


def test_download_clients_list_empty_prompts_adding_app_first(client):
    resp = client.get("/settings/download-clients")
    assert resp.status_code == 200
    assert "Add an app first" in resp.text


def test_download_clients_add_via_app_picker(client, tmp_path):
    app_id = _insert_app(str(tmp_path))

    resp = client.post(
        "/settings/download-clients/new",
        data={
            "app_id": app_id,
            "host": "transmission",
            "port": "9091",
            "stop_on_completion": "on",
            "remove_after_copy": "on",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings/download-clients"

    with session(app_settings.db_path) as conn:
        dc = conn.execute("SELECT * FROM download_clients WHERE app_id = ?", (app_id,)).fetchone()
    assert dc["host"] == "transmission"

    listing = client.get("/settings/download-clients")
    assert "transmission" in listing.text


def test_download_clients_delete(client, tmp_path):
    app_id = _insert_app(str(tmp_path))
    with session(app_settings.db_path) as conn:
        dc_id = conn.execute(
            """INSERT INTO download_clients (app_id, kind, host, port)
               VALUES (?, 'transmission', 'transmission', 9091)""",
            (app_id,),
        ).lastrowid

    resp = client.post(f"/settings/download-clients/{dc_id}/delete", follow_redirects=False)
    assert resp.status_code == 303

    with session(app_settings.db_path) as conn:
        row = conn.execute("SELECT * FROM download_clients WHERE id = ?", (dc_id,)).fetchone()
    assert row is None


def test_system_page_renders_with_no_apps(client):
    resp = client.get("/system")
    assert resp.status_code == 200
    assert "No apps configured yet." in resp.text
    assert "0.1.0" in resp.text  # version


def test_system_page_shows_disk_space_for_configured_paths(client, tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    app_id = _insert_app(str(staging))
    with session(app_settings.db_path) as conn:
        conn.execute("UPDATE apps SET target_root = ? WHERE id = ?", ("", app_id))

    resp = client.get("/system")
    assert resp.status_code == 200
    assert str(staging) in resp.text
    assert "GiB" in resp.text
