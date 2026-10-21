import pytest

from relocarr.arr.base import QueueEntry
from relocarr.clients.base import Torrent
from relocarr.db import init_db, session
from relocarr.jobs.scheduler import _poll_interval, run_app_tick

from .fakes import FakeArrApp, FakeDownloadClient


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "relocarr.db"
    init_db(path)
    return path


def _insert_app(db_path) -> object:
    with session(db_path) as conn:
        conn.execute(
            """INSERT INTO apps (kind, name, base_url, api_key, webhook_token, staging_root, target_root)
               VALUES ('radarr', 'Radarr', 'http://x', 'key', 'token', '/staging', '/target')"""
        )
        return conn.execute("SELECT * FROM apps").fetchone()


def test_run_app_tick_seals_then_advances_jobs_in_one_call(db_path, tmp_path):
    app = _insert_app(db_path)

    # A completed, *arr-tracked torrent whose file is already in staging
    # (as if the *arr had imported it moments ago) and a job already
    # created for it by the webhook receiver.
    source = tmp_path / "staging" / "Movie2024" / "movie.mkv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"data")
    target = tmp_path / "target" / "movie.mkv"

    with session(db_path) as conn:
        conn.execute(
            """INSERT INTO jobs (app_id, download_id, media_file_id, unmonitor_id, source_path, target_path, size_bytes, state)
               VALUES (?, 'HASH1', 'f1', 1, ?, ?, ?, 'IMPORTED')""",
            (app["id"], str(source), str(target), source.stat().st_size),
        )

    # Queue has a matching entry -- required for the SEAL step to touch this
    # torrent at all (Section 4 point 6). That same snapshot is then also
    # what the removal gate sees later in this SAME tick, so realistically
    # the gate must NOT open yet (the entry hasn't gone yet) -- cleanup is
    # expected on a LATER tick, once the queue has actually cleared.
    arr_client = FakeArrApp(queue=[QueueEntry("hash1", "downloading", "ok")], file_paths={"f1": source})
    download_client = FakeDownloadClient(
        torrents=[Torrent(id=9, name="Movie", hash="hash1", percent_done=1.0, status=6, download_dir="/d")]
    )

    with session(db_path) as conn:
        run_app_tick(
            conn,
            app,
            arr_client=arr_client,
            download_client=download_client,
            sidecar_extensions=["srt"],
            verify_mode="size",
        )

    # Sealed (phase A) ...
    assert download_client.stopped == [9]
    with session(db_path) as conn:
        sealed_row = conn.execute(
            "SELECT 1 FROM downloads WHERE app_id = ? AND download_id = 'HASH1' COLLATE NOCASE", (app["id"],)
        ).fetchone()
    assert sealed_row is not None

    # ... and in the same tick, run_once also ran and advanced the job all
    # the way through to RECONCILED (copy + verify + arr delete/unmonitor),
    # but the removal gate correctly withheld cleanup since the queue still
    # shows this download as present.
    with session(db_path) as conn:
        job_state = conn.execute("SELECT state FROM jobs WHERE app_id = ?", (app["id"],)).fetchone()["state"]
    assert job_state == "RECONCILED"
    assert download_client.removed == []


def test_run_app_tick_works_with_no_download_client(db_path, tmp_path):
    # An app with no download client configured yet -- the seal step must
    # be skipped cleanly, not crash, and job advancement still runs.
    app = _insert_app(db_path)
    source = tmp_path / "staging" / "movie.mkv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"data")
    target = tmp_path / "target" / "movie.mkv"

    with session(db_path) as conn:
        conn.execute(
            """INSERT INTO jobs (app_id, download_id, media_file_id, unmonitor_id, source_path, target_path, size_bytes, state)
               VALUES (?, NULL, 'f1', 1, ?, ?, ?, 'IMPORTED')""",
            (app["id"], str(source), str(target), source.stat().st_size),
        )

    arr_client = FakeArrApp(file_paths={"f1": source})

    with session(db_path) as conn:
        run_app_tick(
            conn, app, arr_client=arr_client, download_client=None,
            sidecar_extensions=[], verify_mode="size",
        )

    with session(db_path) as conn:
        job_state = conn.execute("SELECT state FROM jobs WHERE app_id = ?", (app["id"],)).fetchone()["state"]
    assert job_state == "DONE"


def test_poll_interval_reads_from_settings(db_path):
    with session(db_path) as conn:
        conn.execute("UPDATE settings SET value = '42' WHERE key = 'poll_interval_seconds'")
    assert _poll_interval(db_path) == 42.0


def test_poll_interval_falls_back_to_default_if_missing(tmp_path):
    empty_db = tmp_path / "empty.db"
    init_db(empty_db)
    with session(empty_db) as conn:
        conn.execute("DELETE FROM settings WHERE key = 'poll_interval_seconds'")
    assert _poll_interval(empty_db) == 15.0
