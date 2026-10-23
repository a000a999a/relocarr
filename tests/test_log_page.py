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


def _insert_app(kind: str, target_root: str) -> int:
    with session(app_settings.db_path) as conn:
        return conn.execute(
            """INSERT INTO apps (kind, name, base_url, api_key, webhook_token, staging_root, target_root)
               VALUES (?, ?, 'http://x', 'key', 'token', '/s', ?)""",
            (kind, kind.capitalize(), target_root),
        ).lastrowid


_job_counter = iter(range(1, 1000))


def _insert_job(app_id: int, size_bytes: int, state: str = "DONE") -> int:
    media_file_id = f"f{next(_job_counter)}"
    with session(app_settings.db_path) as conn:
        return conn.execute(
            """INSERT INTO jobs (app_id, download_id, media_file_id, unmonitor_id, source_path, target_path, size_bytes, state)
               VALUES (?, NULL, ?, 1, '/s/a.mkv', '/t/a.mkv', ?, ?)""",
            (app_id, media_file_id, size_bytes, state),
        ).lastrowid


def test_log_empty_state(client):
    resp = client.get("/log")
    assert resp.status_code == 200
    assert "No completed transfers yet." in resp.text


def test_log_shows_size_bucket_counts(client):
    app_id = _insert_app("radarr", "/t")
    _insert_job(app_id, int(1 * 1024**3))  # 1 GB -- below every bucket
    _insert_job(app_id, int(2.8 * 1024**3))  # > 2.5 GB only
    _insert_job(app_id, int(3.5 * 1024**3))  # > 2.5 and > 3 GB
    _insert_job(app_id, int(5 * 1024**3))  # > every bucket

    resp = client.get("/log")
    assert resp.status_code == 200
    assert "3 files &gt; 2.5 GB" in resp.text
    assert "2 files &gt; 3 GB" in resp.text
    assert "1 file &gt; 4 GB" in resp.text


def test_log_min_gb_filters_table_rows(client, tmp_path):
    app_id = _insert_app("radarr", "/t")
    small = _insert_job(app_id, int(1 * 1024**3))
    big = _insert_job(app_id, int(5 * 1024**3))

    resp = client.get("/log?min_gb=3")
    assert resp.status_code == 200
    assert "Showing only files larger than 3.0 GB." in resp.text
    assert "5.0 GB" in resp.text
    assert "1.0 GB" not in resp.text


def test_log_capacity_projection_shown_for_radarr_target(client, tmp_path):
    target = tmp_path / "movies"
    target.mkdir()
    _insert_app("radarr", str(target))

    resp = client.get("/log")
    assert resp.status_code == 200
    assert "more movies at 1 GB each" in resp.text


def test_log_capacity_projection_absent_without_radarr_target(client):
    resp = client.get("/log")
    assert resp.status_code == 200
    assert "No Radarr target folder configured yet" in resp.text


def test_log_capacity_projection_ignores_sonarr(client, tmp_path):
    target = tmp_path / "series"
    target.mkdir()
    _insert_app("sonarr", str(target))

    resp = client.get("/log")
    assert resp.status_code == 200
    assert "No Radarr target folder configured yet" in resp.text
