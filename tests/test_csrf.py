import pytest
from fastapi.testclient import TestClient

from relocarr.config import settings as app_settings
from relocarr.db import init_db, session
from relocarr.web.csrf import is_cross_site


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "config_dir", tmp_path)
    init_db(app_settings.db_path)
    from relocarr.main import app

    return TestClient(app)  # sends Host: testserver


def _insert_app() -> int:
    with session(app_settings.db_path) as conn:
        cur = conn.execute(
            """INSERT INTO apps (kind, name, base_url, api_key, webhook_token, staging_root, target_root)
               VALUES ('radarr', 'App', 'http://x', 'key', 'token', '/staging', '')"""
        )
        return cur.lastrowid


@pytest.mark.parametrize(
    "headers, cross",
    [
        ({"host": "um:8686", "origin": "http://um:8686"}, False),
        ({"host": "um:8686", "origin": "http://UM:8686"}, False),
        ({"host": "um:8686", "origin": "http://evil.example"}, True),
        ({"host": "um:8686", "origin": "http://um:9999"}, True),
        ({"host": "um:8686", "origin": "null"}, True),
        ({"host": "um:8686", "referer": "http://um:8686/settings"}, False),
        ({"host": "um:8686", "referer": "http://evil.example/page"}, True),
        ({"host": "relocarr:8686", "x-forwarded-host": "media.home", "origin": "https://media.home"}, False),
        ({"host": "um:8686"}, False),  # no Origin/Referer: not a browser
    ],
)
def test_is_cross_site(headers, cross):
    assert is_cross_site(headers) is cross


def test_cross_site_form_post_is_blocked_and_changes_nothing(client):
    app_id = _insert_app()
    resp = client.post(
        f"/settings/apps/{app_id}",
        data={"target_root": "/somewhere/else"},
        headers={"Origin": "http://evil.example"},
    )
    assert resp.status_code == 403
    with session(app_settings.db_path) as conn:
        row = conn.execute("SELECT target_root FROM apps WHERE id = ?", (app_id,)).fetchone()
    assert row["target_root"] == ""


def test_same_origin_form_post_is_allowed(client):
    app_id = _insert_app()
    resp = client.post(
        f"/settings/apps/{app_id}",
        data={"target_root": "/media/movies"},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert resp.status_code == 303


def test_webhook_is_exempt_and_still_needs_its_token(client):
    app_id = _insert_app()
    resp = client.post(f"/webhook/{app_id}", json={}, headers={"Origin": "http://evil.example"})
    assert resp.status_code == 401  # reached the route; rejected by the token check, not CSRF
