import time

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from relocarr.config import settings as app_settings
from relocarr.db import init_db, session
from relocarr.disk_health import cache as disk_space_cache
from relocarr.library import cache as movie_library_cache
from relocarr.version_check import cache as version_check_cache


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
    assert "1.0.0" in resp.text  # version


def test_system_page_shows_no_download_clients_or_plex_by_default(client):
    resp = client.get("/system")
    assert resp.status_code == 200
    assert "none configured" in resp.text
    assert "not connected" in resp.text


def test_system_page_shows_download_client_count(client):
    app_id = _insert_app("/t")
    with session(app_settings.db_path) as conn:
        conn.execute(
            "INSERT INTO download_clients (app_id, kind, host, port) VALUES (?, 'transmission', 'tx', 9091)",
            (app_id,),
        )

    resp = client.get("/system")
    assert resp.status_code == 200
    assert "1 download client configured" in resp.text


def test_system_page_shows_plex_connected(client):
    with session(app_settings.db_path) as conn:
        conn.execute("UPDATE settings SET value = 'http://plex:32400' WHERE key = 'plex_base_url'")
        conn.execute("UPDATE settings SET value = 'tok' WHERE key = 'plex_token'")

    resp = client.get("/system")
    assert resp.status_code == 200
    assert "Connected" in resp.text


def test_system_page_shows_not_checked_yet_before_the_background_loop_has_run(client):
    resp = client.get("/system")
    assert resp.status_code == 200
    assert "not checked yet" in resp.text


def test_system_page_shows_up_to_date_when_current_version_is_latest(client):
    version_check_cache.latest_version = "v1.0.0"
    version_check_cache.checked_at = 123.0

    resp = client.get("/system")
    assert resp.status_code == 200
    assert "up to date" in resp.text
    assert "available" not in resp.text


def test_system_page_shows_an_update_link_when_a_newer_version_exists(client):
    version_check_cache.latest_version = "v1.1.0"
    version_check_cache.checked_at = 123.0

    resp = client.get("/system")
    assert resp.status_code == 200
    assert "v1.1.0 available" in resp.text
    assert 'href="https://github.com/a000a999a/relocarr/tree/v1.1.0"' in resp.text


def test_system_page_shows_disk_space_for_configured_paths(client, tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    app_id = _insert_app(str(staging))
    with session(app_settings.db_path) as conn:
        conn.execute("UPDATE apps SET target_root = ? WHERE id = ?", ("", app_id))
    # The route only ever reads disk_space_cache now, never a live
    # shutil.disk_usage() call -- see disk_health.py.
    movie_library_cache.scanned_at = time.time()  # keep the sidebar badge quiet for this test
    disk_space_cache.entries[str(staging)] = {"free": 10 * 1024**3, "total": 20 * 1024**3, "ok": True}

    resp = client.get("/system")
    assert resp.status_code == 200
    assert str(staging) in resp.text
    assert "GiB" in resp.text


def test_system_page_shows_disk_space_as_pending_before_first_check(client, tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    app_id = _insert_app(str(staging))
    with session(app_settings.db_path) as conn:
        conn.execute("UPDATE apps SET target_root = ? WHERE id = ?", ("", app_id))

    resp = client.get("/system")
    assert resp.status_code == 200
    assert "not checked yet" in resp.text


def test_system_page_shows_disk_space_error_from_cache(client, tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    app_id = _insert_app(str(staging))
    with session(app_settings.db_path) as conn:
        conn.execute("UPDATE apps SET target_root = ? WHERE id = ?", ("", app_id))
    disk_space_cache.entries[str(staging)] = {"ok": False, "error": "[Errno 112] Host is down"}

    resp = client.get("/system")
    assert resp.status_code == 200
    assert "Host is down" in resp.text


def test_setup_guide_renders(client):
    resp = client.get("/setup-guide")
    assert resp.status_code == 200
    assert "Radarr" in resp.text
    assert "Sonarr" in resp.text
    assert "Transmission" in resp.text
    assert "Completed Download Handling" in resp.text


def test_system_page_shows_scan_not_yet_run(client):
    resp = client.get("/system")
    assert resp.status_code == 200
    assert "Movie library has not been scanned yet." in resp.text


def test_system_page_shows_scan_ok_after_a_recent_success(client):
    movie_library_cache.scanned_at = time.time()
    resp = client.get("/system")
    assert resp.status_code == 200
    assert ">OK<" in resp.text


def test_system_page_shows_stale_scan_as_an_error_with_restart_instructions(client):
    # Older than 3x the (10 min) scan interval -- stale enough to suggest
    # a wedged mount, not just "hasn't had its turn yet."
    movie_library_cache.scanned_at = time.time() - 2000
    resp = client.get("/system")
    assert resp.status_code == 200
    assert "may be stuck on an unreachable mount" in resp.text
    assert "docker restart relocarr" in resp.text


def test_system_page_shows_unreachable_roots(client):
    movie_library_cache.scanned_at = time.time()
    movie_library_cache.unreachable_roots = ["/mnt/nvidia/movies"]
    resp = client.get("/system")
    assert resp.status_code == 200
    assert "/mnt/nvidia/movies" in resp.text


def test_sidebar_badge_hidden_when_nothing_wrong(client):
    movie_library_cache.scanned_at = time.time()
    resp = client.get("/log")
    assert resp.status_code == 200
    assert '<span class="nav-badge' not in resp.text  # CSS class def is always present; the badge element itself shouldn't be


def test_sidebar_badge_shows_for_needs_attention_jobs(client, tmp_path):
    movie_library_cache.scanned_at = time.time()
    app_id = _insert_app(str(tmp_path))
    with session(app_settings.db_path) as conn:
        conn.execute(
            """INSERT INTO jobs (app_id, download_id, media_file_id, unmonitor_id, source_path, target_path, size_bytes, state)
               VALUES (?, NULL, 'f1', 1, '/s/a.mkv', '/t/a.mkv', 1000, 'NEEDS_ATTENTION')""",
            (app_id,),
        )

    resp = client.get("/log")
    assert resp.status_code == 200
    assert 'class="nav-badge error"' in resp.text
    assert ">1<" in resp.text


def test_sidebar_badge_warns_before_the_first_scan(client):
    resp = client.get("/log")
    assert resp.status_code == 200
    assert 'class="nav-badge warning"' in resp.text


def test_sidebar_badge_shows_error_for_unreachable_disk(client):
    movie_library_cache.scanned_at = time.time()  # keep the scan signal quiet for this test
    disk_space_cache.entries["/mnt/nvidia/movies"] = {"ok": False, "error": "Host is down"}

    resp = client.get("/log")
    assert resp.status_code == 200
    assert 'class="nav-badge error"' in resp.text


def test_add_app_page_prompts_to_connect_plex_when_not_configured(client):
    resp = client.get("/settings/apps/new")
    assert resp.status_code == 200
    assert "Connect Plex" in resp.text
    assert "Connected." not in resp.text


def test_add_app_page_shows_plex_connected_status(client):
    with session(app_settings.db_path) as conn:
        conn.execute("UPDATE settings SET value = 'http://plex:32400' WHERE key = 'plex_base_url'")
        conn.execute("UPDATE settings SET value = 'tok' WHERE key = 'plex_token'")

    resp = client.get("/settings/apps/new")
    assert resp.status_code == 200
    assert "Connected." in resp.text


def test_app_config_page_prompts_to_connect_plex_when_not_configured(client):
    resp = client.get("/settings/apps")
    assert resp.status_code == 200
    assert "+ Connect Plex" in resp.text


def test_app_config_page_shows_plex_connected_status(client):
    with session(app_settings.db_path) as conn:
        conn.execute("UPDATE settings SET value = 'http://plex:32400' WHERE key = 'plex_base_url'")
        conn.execute("UPDATE settings SET value = 'tok' WHERE key = 'plex_token'")

    resp = client.get("/settings/apps")
    assert resp.status_code == 200
    assert "Connected." in resp.text


def test_plex_settings_page_renders_with_defaults(client):
    resp = client.get("/settings/plex")
    assert resp.status_code == 200
    assert "Plex" in resp.text


@respx.mock
def test_plex_settings_save_tests_the_connection_and_saves_on_success(client):
    respx.get("http://plex:32400/library/sections").mock(
        return_value=httpx.Response(200, json={"MediaContainer": {"version": "1.40.0"}})
    )

    resp = client.post(
        "/settings/plex",
        data={"plex_base_url": "http://plex:32400", "plex_token": "tok"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    with session(app_settings.db_path) as conn:
        rows = {
            r["key"]: r["value"]
            for r in conn.execute(
                "SELECT key, value FROM settings WHERE key IN ('plex_base_url', 'plex_token')"
            ).fetchall()
        }
    assert rows["plex_base_url"] == "http://plex:32400"
    assert rows["plex_token"] == "tok"


@respx.mock
def test_plex_settings_save_strips_a_pasted_x_plex_token_prefix(client):
    # Confirmed live, 2026-10-25: a real user pasted the whole
    # "X-Plex-Token=<value>" query-string fragment instead of just the
    # value, and the old /identity-based check didn't catch it.
    route = respx.get("http://plex:32400/library/sections").mock(
        return_value=httpx.Response(200, json={"MediaContainer": {"version": "1.40.0"}})
    )

    resp = client.post(
        "/settings/plex",
        data={"plex_base_url": "http://plex:32400", "plex_token": "X-Plex-Token=realtoken123"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert route.calls.last.request.headers["X-Plex-Token"] == "realtoken123"

    with session(app_settings.db_path) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = 'plex_token'").fetchone()
    assert row["value"] == "realtoken123"


@respx.mock
def test_plex_settings_save_does_not_persist_on_a_failed_connection(client):
    respx.get("http://plex:32400/library/sections").mock(return_value=httpx.Response(401))

    resp = client.post(
        "/settings/plex", data={"plex_base_url": "http://plex:32400", "plex_token": "bad"}
    )
    assert resp.status_code == 200
    assert "Connection failed" in resp.text

    with session(app_settings.db_path) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = 'plex_token'").fetchone()
    assert row["value"] == ""  # still the default -- the failed attempt was never saved
