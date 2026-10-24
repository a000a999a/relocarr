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


def _insert_app_and_job(state: str, target_path: str = "/t/a.mkv") -> tuple[int, int]:
    with session(app_settings.db_path) as conn:
        app_id = conn.execute(
            """INSERT INTO apps (kind, name, base_url, api_key, webhook_token, staging_root, target_root)
               VALUES ('radarr', 'Radarr', 'http://x', 'key', 'token', '/s', '/t')"""
        ).lastrowid
        job_id = conn.execute(
            """INSERT INTO jobs (app_id, download_id, media_file_id, unmonitor_id, source_path, target_path, size_bytes, state, error)
               VALUES (?, NULL, 'f1', 1, '/s/a.mkv', ?, 1000, ?, 'boom')""",
            (app_id, target_path, state),
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


def test_abort_deletes_a_copying_job(client):
    _, job_id = _insert_app_and_job("COPYING")
    resp = client.post(f"/jobs/{job_id}/abort", follow_redirects=False)
    assert resp.status_code == 303

    with session(app_settings.db_path) as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert job is None


def test_abort_deletes_an_imported_job(client):
    _, job_id = _insert_app_and_job("IMPORTED")
    client.post(f"/jobs/{job_id}/abort", follow_redirects=False)

    with session(app_settings.db_path) as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert job is None


def test_abort_deletes_a_needs_attention_job(client):
    _, job_id = _insert_app_and_job("NEEDS_ATTENTION")
    client.post(f"/jobs/{job_id}/abort", follow_redirects=False)

    with session(app_settings.db_path) as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert job is None


def test_abort_deletes_a_partial_file_left_at_the_destination(client, tmp_path):
    target = tmp_path / "Movie.mkv"
    target.write_bytes(b"partial data from an interrupted rsync --partial transfer")
    _, job_id = _insert_app_and_job("COPYING", target_path=str(target))

    client.post(f"/jobs/{job_id}/abort", follow_redirects=False)

    assert not target.exists()


def test_abort_does_nothing_if_no_file_is_at_the_destination(client, tmp_path):
    target = tmp_path / "never-got-this-far.mkv"
    _, job_id = _insert_app_and_job("COPYING", target_path=str(target))

    resp = client.post(f"/jobs/{job_id}/abort", follow_redirects=False)
    assert resp.status_code == 303  # no crash just because there was nothing to clean up


def test_abort_is_not_available_once_published(client):
    # The removal gate still needs this job row to correlate sibling jobs
    # for torrent removal -- deleting it here would leave that orphaned.
    _, job_id = _insert_app_and_job("PUBLISHED")
    client.post(f"/jobs/{job_id}/abort", follow_redirects=False)

    with session(app_settings.db_path) as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert job is not None
    assert job["state"] == "PUBLISHED"


def test_jobs_list_shows_abort_button_for_copying_job(client):
    _insert_app_and_job("COPYING")
    resp = client.get("/jobs")
    assert resp.status_code == 200
    assert "Abort" in resp.text


def test_abort_never_deletes_a_file_another_completed_job_owns(client, tmp_path):
    # The real bug caught live, 2026-10-24: a duplicate job can share the
    # exact target_path of an already-finished job (e.g. a second webhook
    # for the same movie). Aborting the stuck duplicate must never delete
    # the OTHER job's legitimate, completed file.
    shared_target = tmp_path / "Movie.mkv"
    shared_target.write_bytes(b"the real, already-finished file")

    app_id, done_job_id = _insert_app_and_job("DONE", target_path=str(shared_target))
    with session(app_settings.db_path) as conn:
        stuck_job_id = conn.execute(
            """INSERT INTO jobs (app_id, download_id, media_file_id, unmonitor_id, source_path, target_path, size_bytes, state, error)
               VALUES (?, NULL, 'f2', 1, '/s/b.mkv', ?, 1000, 'COPYING', 'boom')""",
            (app_id, str(shared_target)),
        ).lastrowid

    client.post(f"/jobs/{stuck_job_id}/abort", follow_redirects=False)

    assert shared_target.exists()  # the other job's file must survive
    assert shared_target.read_bytes() == b"the real, already-finished file"

    with session(app_settings.db_path) as conn:
        stuck = conn.execute("SELECT * FROM jobs WHERE id = ?", (stuck_job_id,)).fetchone()
        done = conn.execute("SELECT * FROM jobs WHERE id = ?", (done_job_id,)).fetchone()
    assert stuck is None  # the stuck job row is still removed
    assert done is not None  # the other job's own row is untouched
