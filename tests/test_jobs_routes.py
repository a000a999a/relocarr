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


def _insert_app_and_job(state: str) -> tuple[int, int]:
    with session(app_settings.db_path) as conn:
        app_id = conn.execute(
            """INSERT INTO apps (kind, name, base_url, api_key, webhook_token, staging_root, target_root)
               VALUES ('radarr', 'Radarr', 'http://x', 'key', 'token', '/s', '/t')"""
        ).lastrowid
        job_id = conn.execute(
            """INSERT INTO jobs (app_id, download_id, media_file_id, unmonitor_id, source_path, target_path, size_bytes, state, error)
               VALUES (?, NULL, 'f1', 1, '/s/a.mkv', '/t/a.mkv', 1000, ?, 'boom')""",
            (app_id, state),
        ).lastrowid
        return app_id, job_id


def test_jobs_list_renders(client):
    _insert_app_and_job("NEEDS_ATTENTION")
    resp = client.get("/jobs")
    assert resp.status_code == 200
    assert "boom" in resp.text
    assert "Retry" in resp.text
    assert "Skip" in resp.text


def test_retry_resets_needs_attention_job_to_imported(client):
    _, job_id = _insert_app_and_job("NEEDS_ATTENTION")
    resp = client.post(f"/jobs/{job_id}/retry", follow_redirects=False)
    assert resp.status_code == 303

    with session(app_settings.db_path) as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert job["state"] == "IMPORTED"
    assert job["error"] is None


def test_retry_does_nothing_to_a_job_not_in_needs_attention(client):
    _, job_id = _insert_app_and_job("COPYING")
    client.post(f"/jobs/{job_id}/retry", follow_redirects=False)

    with session(app_settings.db_path) as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert job["state"] == "COPYING"  # untouched -- retry only applies to NEEDS_ATTENTION


def test_skip_marks_needs_attention_job_skipped(client):
    _, job_id = _insert_app_and_job("NEEDS_ATTENTION")
    resp = client.post(f"/jobs/{job_id}/skip", follow_redirects=False)
    assert resp.status_code == 303

    with session(app_settings.db_path) as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert job["state"] == "SKIPPED"
