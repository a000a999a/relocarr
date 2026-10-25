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


def _insert_app_with_client(download_id: str = "ABCDEF123") -> tuple[int, int]:
    """A radarr app with a Transmission download client and one DONE job
    carrying `download_id` -- the minimum needed for a cleanup-torrents
    candidate to exist.
    """
    with session(app_settings.db_path) as conn:
        app_id = conn.execute(
            """INSERT INTO apps (kind, name, base_url, api_key, webhook_token, staging_root, target_root)
               VALUES ('radarr', 'Radarr', 'http://x', 'key', 'token', '/s', '/t')"""
        ).lastrowid
        conn.execute(
            """INSERT INTO download_clients (app_id, kind, host, port)
               VALUES (?, 'transmission', 'tx-host', 9091)""",
            (app_id,),
        )
        job_id = conn.execute(
            """INSERT INTO jobs (app_id, download_id, media_file_id, unmonitor_id, source_path, target_path, size_bytes, state)
               VALUES (?, ?, 'f1', 1, '/s/a.mkv', '/t/a.mkv', 2000, 'DONE')""",
            (app_id, download_id),
        ).lastrowid
        return app_id, job_id


def _torrent_get_response(torrents: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"result": "success", "arguments": {"torrents": torrents}})


@respx.mock
def test_cleanup_torrents_lists_a_torrent_backed_by_a_done_job(client):
    _insert_app_with_client(download_id="ABCDEF123")
    respx.post("http://tx-host:9091/transmission/rpc/").mock(
        return_value=_torrent_get_response(
            [
                {
                    "id": 5,
                    "name": "Example Movie",
                    # Transmission's hashString comes back lowercase, the
                    # *arr's downloadId is uppercase -- compared
                    # case-insensitively, same as watcher.py.
                    "hashString": "abcdef123",
                    "percentDone": 1.0,
                    "status": 0,
                    "downloadDir": "/downloads",
                }
            ]
        )
    )

    resp = client.get("/jobs/cleanup-torrents")
    assert resp.status_code == 200
    assert "Example Movie" in resp.text


@respx.mock
def test_cleanup_torrents_ignores_a_torrent_with_no_done_job(client):
    _insert_app_with_client(download_id="ABCDEF123")
    respx.post("http://tx-host:9091/transmission/rpc/").mock(
        return_value=_torrent_get_response(
            [
                {
                    "id": 9,
                    "name": "Unrelated Torrent",
                    "hashString": "zzzzzzzzz",  # does not match the DONE job's download_id
                    "percentDone": 1.0,
                    "status": 0,
                    "downloadDir": "/downloads",
                }
            ]
        )
    )

    resp = client.get("/jobs/cleanup-torrents")
    assert resp.status_code == 200
    assert "Unrelated Torrent" not in resp.text
    assert "No leftover torrents found" in resp.text


@respx.mock
def test_cleanup_torrents_delete_removes_checked_torrents(client):
    app_id, _ = _insert_app_with_client()
    remove_route = respx.post("http://tx-host:9091/transmission/rpc/").mock(
        return_value=httpx.Response(200, json={"result": "success", "arguments": {}})
    )

    resp = client.post(
        "/jobs/cleanup-torrents/delete", data={"torrents": [f"{app_id}|5"]}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert remove_route.called
    import json

    body = json.loads(remove_route.calls[0].request.content)
    assert body == {"method": "torrent-remove", "arguments": {"ids": [5], "delete-local-data": True}}


def test_cleanup_torrents_delete_with_nothing_checked_deletes_nothing(client):
    resp = client.post("/jobs/cleanup-torrents/delete", data={}, follow_redirects=False)
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]


def _insert_radarr_app(with_client: bool = False) -> int:
    with session(app_settings.db_path) as conn:
        app_id = conn.execute(
            """INSERT INTO apps (kind, name, base_url, api_key, webhook_token, staging_root, target_root)
               VALUES ('radarr', 'Radarr', 'http://x', 'key', 'token', '/s', '/t')"""
        ).lastrowid
        if with_client:
            conn.execute(
                """INSERT INTO download_clients (app_id, kind, host, port)
                   VALUES (?, 'transmission', 'tx-host', 9091)""",
                (app_id,),
            )
        return app_id


def _insert_done_job(app_id: int, source_path: str, *, download_id: str | None = None) -> int:
    with session(app_settings.db_path) as conn:
        return conn.execute(
            """INSERT INTO jobs (app_id, download_id, media_file_id, unmonitor_id, source_path, target_path, size_bytes, state)
               VALUES (?, ?, 'f1', 1, ?, '/t/a.mkv', 2000, 'DONE')""",
            (app_id, download_id, source_path),
        ).lastrowid


def test_cleanup_source_lists_an_orphaned_file_without_a_seeding_badge(client, tmp_path):
    source = tmp_path / "Movie.mkv"
    source.write_bytes(b"x")
    app_id = _insert_radarr_app()
    _insert_done_job(app_id, str(source))

    resp = client.get("/jobs/cleanup-source")
    assert resp.status_code == 200
    assert "Movie.mkv" in resp.text
    assert "btn-primary);\">seeding</span>" not in resp.text


@respx.mock
def test_cleanup_source_marks_a_live_torrent_as_seeding(client, tmp_path):
    source = tmp_path / "Movie.mkv"
    source.write_bytes(b"x")
    app_id = _insert_radarr_app(with_client=True)
    _insert_done_job(app_id, str(source), download_id="ABCDEF123")
    respx.post("http://tx-host:9091/transmission/rpc/").mock(
        return_value=_torrent_get_response(
            [
                {
                    "id": 5,
                    "name": "Movie",
                    "hashString": "abcdef123",
                    "percentDone": 1.0,
                    "status": 6,
                    "downloadDir": "/downloads",
                }
            ]
        )
    )

    resp = client.get("/jobs/cleanup-source")
    assert resp.status_code == 200
    assert "Movie.mkv" in resp.text
    assert "btn-primary);\">seeding</span>" in resp.text


def test_cleanup_source_ignores_a_done_job_whose_file_is_already_gone(client, tmp_path):
    missing = tmp_path / "gone.mkv"  # never created
    app_id = _insert_radarr_app()
    _insert_done_job(app_id, str(missing))

    resp = client.get("/jobs/cleanup-source")
    assert resp.status_code == 200
    assert "No transferred movies left on source" in resp.text


def test_cleanup_source_delete_removes_file_directly_when_not_seeding(client, tmp_path):
    source = tmp_path / "Movie.mkv"
    source.write_bytes(b"x")
    app_id = _insert_radarr_app()
    job_id = _insert_done_job(app_id, str(source))

    resp = client.post(
        "/jobs/cleanup-source/delete", data={"job_ids": [str(job_id)]}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert not source.exists()


@respx.mock
def test_cleanup_source_delete_removes_via_transmission_when_seeding(client, tmp_path):
    source = tmp_path / "Movie.mkv"
    source.write_bytes(b"x")
    app_id = _insert_radarr_app(with_client=True)
    job_id = _insert_done_job(app_id, str(source), download_id="ABCDEF123")
    route = respx.post("http://tx-host:9091/transmission/rpc/").mock(
        side_effect=[
            _torrent_get_response(
                [
                    {
                        "id": 5,
                        "name": "Movie",
                        "hashString": "abcdef123",
                        "percentDone": 1.0,
                        "status": 6,
                        "downloadDir": "/downloads",
                    }
                ]
            ),
            httpx.Response(200, json={"result": "success", "arguments": {}}),
        ]
    )

    resp = client.post(
        "/jobs/cleanup-source/delete", data={"job_ids": [str(job_id)]}, follow_redirects=False
    )
    assert resp.status_code == 303
    # Removed through Transmission, not a direct unlink -- the mocked RPC
    # never touches the real filesystem, so the file surviving confirms
    # the direct-delete branch was correctly skipped.
    assert source.exists()

    import json

    body = json.loads(route.calls[-1].request.content)
    assert body == {"method": "torrent-remove", "arguments": {"ids": [5], "delete-local-data": True}}


def test_cleanup_source_delete_with_nothing_checked_deletes_nothing(client):
    resp = client.post("/jobs/cleanup-source/delete", data={}, follow_redirects=False)
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]


def _insert_radarr_app_with_paths(staging_root: str, target_root: str, base_url: str) -> int:
    with session(app_settings.db_path) as conn:
        return conn.execute(
            """INSERT INTO apps (kind, name, base_url, api_key, webhook_token, staging_root, target_root)
               VALUES ('radarr', 'Radarr', ?, 'key', 'token', ?, ?)""",
            (base_url, staging_root, target_root),
        ).lastrowid


@respx.mock
def test_reconcile_lists_an_untracked_radarr_import(client, tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    movie_file = staging / "Movie.mkv"
    movie_file.write_bytes(b"x" * 2000)

    _insert_radarr_app_with_paths(str(staging), str(target), base_url="http://radarr-recon1")
    respx.get("http://radarr-recon1/api/v3/movie").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 10,
                    "title": "Movie",
                    "year": 2020,
                    "movieFile": {"id": 55, "path": str(movie_file), "size": 2000},
                }
            ],
        )
    )

    resp = client.get("/jobs/reconcile-missing")
    assert resp.status_code == 200
    assert "Movie.mkv" in resp.text


@respx.mock
def test_reconcile_excludes_a_movie_with_no_file(client, tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    target = tmp_path / "target"
    target.mkdir()

    _insert_radarr_app_with_paths(str(staging), str(target), base_url="http://radarr-recon-nofile")
    respx.get("http://radarr-recon-nofile/api/v3/movie").mock(
        return_value=httpx.Response(200, json=[{"id": 10, "title": "Movie", "year": 2020}])
    )

    resp = client.get("/jobs/reconcile-missing")
    assert resp.status_code == 200
    assert "Nothing untracked right now" in resp.text


@respx.mock
def test_reconcile_excludes_a_file_already_tracked_by_a_job(client, tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    movie_file = staging / "Movie.mkv"
    movie_file.write_bytes(b"x" * 2000)

    app_id = _insert_radarr_app_with_paths(str(staging), str(target), base_url="http://radarr-recon2")
    with session(app_settings.db_path) as conn:
        conn.execute(
            """INSERT INTO jobs (app_id, download_id, media_file_id, unmonitor_id, source_path, target_path, size_bytes, state)
               VALUES (?, NULL, '55', 10, ?, ?, 2000, 'DONE')""",
            (app_id, str(movie_file), str(target / "Movie.mkv")),
        )
    respx.get("http://radarr-recon2/api/v3/movie").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 10,
                    "title": "Movie",
                    "year": 2020,
                    "movieFile": {"id": 55, "path": str(movie_file), "size": 2000},
                }
            ],
        )
    )

    resp = client.get("/jobs/reconcile-missing")
    assert resp.status_code == 200
    assert "Nothing untracked right now" in resp.text


@respx.mock
def test_reconcile_create_creates_an_imported_job(client, tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    movie_file = staging / "Movie.mkv"
    movie_file.write_bytes(b"x" * 2000)

    app_id = _insert_radarr_app_with_paths(str(staging), str(target), base_url="http://radarr-recon3")
    respx.get("http://radarr-recon3/api/v3/movie").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 10,
                    "title": "Movie",
                    "year": 2020,
                    "movieFile": {"id": 55, "path": str(movie_file), "size": 2000},
                }
            ],
        )
    )

    resp = client.post(
        "/jobs/reconcile-missing/create", data={"candidates": [f"{app_id}|10|55"]}, follow_redirects=False
    )
    assert resp.status_code == 303

    with session(app_settings.db_path) as conn:
        job = conn.execute(
            "SELECT * FROM jobs WHERE app_id = ? AND media_file_id = '55'", (app_id,)
        ).fetchone()
    assert job is not None
    assert job["state"] == "IMPORTED"
    assert job["download_id"] is None
    assert job["source_path"] == str(movie_file)
    assert job["target_path"] == str(target / "Movie.mkv")


@respx.mock
def test_reconcile_create_respects_max_file_size(client, tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    movie_file = staging / "Movie.mkv"
    movie_file.write_bytes(b"x" * 2000)

    with session(app_settings.db_path) as conn:
        app_id = conn.execute(
            """INSERT INTO apps (kind, name, base_url, api_key, webhook_token, staging_root, target_root, max_file_size_bytes)
               VALUES ('radarr', 'Radarr', 'http://radarr-recon4', 'key', 'token', ?, ?, 1000)""",
            (str(staging), str(target)),
        ).lastrowid
    respx.get("http://radarr-recon4/api/v3/movie").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 10,
                    "title": "Movie",
                    "year": 2020,
                    "movieFile": {"id": 55, "path": str(movie_file), "size": 2000},
                }
            ],
        )
    )

    client.post(
        "/jobs/reconcile-missing/create", data={"candidates": [f"{app_id}|10|55"]}, follow_redirects=False
    )

    with session(app_settings.db_path) as conn:
        job = conn.execute(
            "SELECT * FROM jobs WHERE app_id = ? AND media_file_id = '55'", (app_id,)
        ).fetchone()
    assert job["state"] == "NEEDS_ATTENTION"


@respx.mock
def test_reconcile_create_skips_a_file_that_changed_since_listing(client, tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    movie_file = staging / "Movie.mkv"
    movie_file.write_bytes(b"x" * 2000)

    app_id = _insert_radarr_app_with_paths(str(staging), str(target), base_url="http://radarr-recon5")
    # Radarr now reports a different (newer) file id than what was checked.
    respx.get("http://radarr-recon5/api/v3/movie").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 10,
                    "title": "Movie",
                    "year": 2020,
                    "movieFile": {"id": 99, "path": str(movie_file), "size": 2000},
                }
            ],
        )
    )

    resp = client.post(
        "/jobs/reconcile-missing/create", data={"candidates": [f"{app_id}|10|55"]}, follow_redirects=False
    )
    assert resp.status_code == 303

    with session(app_settings.db_path) as conn:
        job = conn.execute(
            "SELECT * FROM jobs WHERE app_id = ? AND media_file_id = '55'", (app_id,)
        ).fetchone()
    assert job is None  # stale candidate -- correctly skipped, not created


def test_reconcile_create_with_nothing_checked_creates_nothing(client):
    resp = client.post("/jobs/reconcile-missing/create", data={}, follow_redirects=False)
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
