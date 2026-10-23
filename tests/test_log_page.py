import time

import pytest
from fastapi.testclient import TestClient

from relocarr.config import settings as app_settings
from relocarr.db import init_db, session
from relocarr.library import cache as movie_library_cache


@pytest.fixture(autouse=True)
def _reset_movie_library_cache():
    # The cache is a module-level singleton shared by the real background
    # scan loop -- reset it around every test so one test's fake library
    # contents can't leak into another's assertions.
    movie_library_cache.files = []
    movie_library_cache.scanned_at = None
    yield
    movie_library_cache.files = []
    movie_library_cache.scanned_at = None


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


def test_log_size_breakdown_shows_scanning_message_before_first_scan(client):
    resp = client.get("/log")
    assert resp.status_code == 200
    assert "scanning the movie folder for the first time" in resp.text


def test_log_size_breakdown_counts_come_from_the_scanned_library_not_job_history(client):
    # Job history has one small transfer; the "library" (the cache a
    # background scan would populate) has the real, much larger set of
    # files actually sitting in the movie folder. Counts must reflect the
    # library scan, not the job log.
    app_id = _insert_app("radarr", "/t")
    _insert_job(app_id, int(1 * 1024**3))  # unrelated to the breakdown now

    movie_library_cache.files = [
        {"name": "small.mkv", "app_name": "Radarr", "size_bytes": int(1 * 1024**3)},
        {"name": "two-eight.mkv", "app_name": "Radarr", "size_bytes": int(2.8 * 1024**3)},
        {"name": "three-five.mkv", "app_name": "Radarr", "size_bytes": int(3.5 * 1024**3)},
        {"name": "five.mkv", "app_name": "Radarr", "size_bytes": int(5 * 1024**3)},
    ]
    movie_library_cache.scanned_at = time.time()

    resp = client.get("/log")
    assert resp.status_code == 200
    assert "3 files &gt; 2.5 GB" in resp.text
    assert "2 files &gt; 3 GB" in resp.text
    assert "1 file &gt; 4 GB" in resp.text


def test_log_min_gb_lists_matching_library_files(client):
    movie_library_cache.files = [
        {"name": "small.mkv", "app_name": "Radarr", "size_bytes": int(1 * 1024**3)},
        {"name": "big.mkv", "app_name": "Radarr", "size_bytes": int(5 * 1024**3)},
    ]
    movie_library_cache.scanned_at = time.time()

    resp = client.get("/log?min_gb=3")
    assert resp.status_code == 200
    assert "Movie files &gt; 3.0 GB" in resp.text
    assert "big.mkv" in resp.text
    assert "small.mkv" not in resp.text


def test_log_min_gb_with_no_matches_shows_empty_state(client):
    movie_library_cache.files = [
        {"name": "small.mkv", "app_name": "Radarr", "size_bytes": int(1 * 1024**3)},
    ]
    movie_library_cache.scanned_at = time.time()

    resp = client.get("/log?min_gb=3")
    assert resp.status_code == 200
    assert "No movie files larger than 3.0 GB." in resp.text


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
