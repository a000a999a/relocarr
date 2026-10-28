import pytest

from relocarr.arr.base import QueueEntry
from relocarr.clients.base import Torrent
from relocarr.db import init_db, session
from relocarr.jobs.scheduler import _poll_interval, run_app_seal_tick, run_app_tick

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


def _insert_job(db_path, app_id, *, media_file_id, source_path, target_path, download_id=None) -> int:
    size = source_path.stat().st_size if source_path.exists() else 0
    with session(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO jobs (app_id, download_id, media_file_id, unmonitor_id, source_path, target_path, size_bytes, state)
               VALUES (?, ?, ?, 1, ?, ?, ?, 'IMPORTED')""",
            (app_id, download_id, media_file_id, str(source_path), str(target_path), size),
        )
        return cur.lastrowid


def _state(db_path, job_id):
    with session(db_path) as conn:
        return conn.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()["state"]


def test_run_app_seal_tick_seals_a_completed_tracked_torrent(db_path, tmp_path):
    # Split out 2026-10-28 from a combined seal+copy test, after a real
    # incident: once rsync copies started being throttled (--bwlimit, the
    # Shield-OOM fix), a backlog of large files could keep run_app_tick
    # (job processing) blocked for minutes, during which a completely
    # different, already-completed torrent sat seeding -- unsealed --
    # because sealing used to only run as the first step of that same
    # call. Sealing now runs via its own function, on its own loop
    # (run_seal_forever), independent of job processing entirely.
    app = _insert_app(db_path)

    arr_client = FakeArrApp(queue=[QueueEntry("hash1", "downloading", "ok")])
    download_client = FakeDownloadClient(
        torrents=[Torrent(id=9, name="Movie", hash="hash1", percent_done=1.0, status=6, download_dir="/d")]
    )

    with session(db_path) as conn:
        run_app_seal_tick(conn, app, arr_client=arr_client, download_client=download_client)

    assert download_client.stopped == [9]
    with session(db_path) as conn:
        sealed_row = conn.execute(
            "SELECT 1 FROM downloads WHERE app_id = ? AND download_id = 'HASH1' COLLATE NOCASE", (app["id"],)
        ).fetchone()
    assert sealed_row is not None


def test_run_app_seal_tick_does_nothing_with_no_download_client(db_path):
    app = _insert_app(db_path)
    arr_client = FakeArrApp(queue=[QueueEntry("hash1", "downloading", "ok")])

    with session(db_path) as conn:
        run_app_seal_tick(conn, app, arr_client=arr_client, download_client=None)
    # No exception is the assertion here -- there's nothing else to check
    # with no download client to have stopped anything on.


def test_run_app_tick_advances_jobs_without_sealing(db_path, tmp_path):
    # The other half of the split above: run_app_tick (job processing)
    # must advance a job fully on its own, and must NOT also seal --
    # that's run_app_seal_tick's job now, on its own independent loop.
    app = _insert_app(db_path)

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

    # Queue has a matching entry, and the torrent is already complete --
    # if run_app_tick still sealed internally, this would get stopped;
    # the whole point of this test is that it must not.
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

    assert download_client.stopped == []  # run_app_tick never seals
    with session(db_path) as conn:
        sealed_row = conn.execute(
            "SELECT 1 FROM downloads WHERE app_id = ? AND download_id = 'HASH1' COLLATE NOCASE", (app["id"],)
        ).fetchone()
    assert sealed_row is None

    # The job still advances all the way through to RECONCILED (copy +
    # verify + arr delete/unmonitor) via the removal gate inside run_once
    # -- but withholds cleanup since the queue still shows this download
    # present.
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


def test_run_app_tick_dry_run_performs_no_real_action(db_path, tmp_path):
    app = _insert_app(db_path)
    source = tmp_path / "staging" / "Movie2024" / "movie.mkv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"data")
    target = tmp_path / "target" / "movie.mkv"
    job_id = _insert_job(db_path, app["id"], media_file_id="f1", source_path=source, target_path=target)

    arr_client = FakeArrApp(file_paths={"f1": source})

    with session(db_path) as conn:
        run_app_tick(
            conn, app, arr_client=arr_client, download_client=None,
            sidecar_extensions=["srt"], verify_mode="size", dry_run=True,
        )

    assert not target.exists()  # nothing was actually copied
    assert source.exists()  # nothing was actually deleted
    assert arr_client.deleted_files == []
    assert _state(db_path, job_id) == "IMPORTED"  # untouched -- dry-run logs, never mutates


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
