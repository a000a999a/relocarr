import pytest

from relocarr.arr.base import QueueEntry
from relocarr.clients.base import Torrent
from relocarr.db import init_db, session
from relocarr.jobs.worker import run_once

from .fakes import FakeArrApp, FakeDownloadClient


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "relocarr.db"
    init_db(path)
    return path


def _insert_app(db_path) -> int:
    with session(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO apps (kind, name, base_url, api_key, webhook_token, staging_root, target_root)
               VALUES ('radarr', 'Radarr', 'http://x', 'key', 'token', '/staging', '/target')"""
        )
        return cur.lastrowid


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


def test_manual_import_job_reaches_done_in_one_pass(db_path, tmp_path):
    # No download_id -- skips the removal gate entirely (spike 2 finding).
    app_id = _insert_app(db_path)
    source = tmp_path / "staging" / "Movie2024" / "movie.mkv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"data")
    target = tmp_path / "target" / "movie.mkv"
    job_id = _insert_job(db_path, app_id, media_file_id="f1", source_path=source, target_path=target)

    # file_paths lets the fake actually unlink the source on delete_file(),
    # mirroring what Radarr's real DELETE endpoint does (spike 8) -- without
    # this the folder would never look empty to advance_clean.
    arr = FakeArrApp(file_paths={"f1": source})
    with session(db_path) as conn:
        run_once(
            conn,
            app_id=app_id,
            arr_client=arr,
            download_client=None,
            sidecar_extensions=["srt"],
            verify_mode="size",
        )

    assert _state(db_path, job_id) == "DONE"
    assert not source.parent.exists()  # pruned once empty
    assert arr.deleted_files == ["f1"]


def test_removal_gate_blocks_until_all_season_pack_jobs_reconciled(db_path, tmp_path):
    # Season pack: ep1's file exists and copies fine; ep2's does not, so its
    # copy fails permanently (NEEDS_ATTENTION) -- not a terminal/reconciled
    # state, so the gate must stay closed for BOTH siblings even though ep1
    # is individually done.
    app_id = _insert_app(db_path)
    staging = tmp_path / "staging" / "Show" / "Season 1"
    staging.mkdir(parents=True)

    ep1 = staging / "ep1.mkv"
    ep1.write_bytes(b"a")
    ep2 = staging / "ep2.mkv"  # deliberately never created

    job1 = _insert_job(
        db_path, app_id, media_file_id="f1",
        source_path=ep1, target_path=tmp_path / "target" / "ep1.mkv", download_id="HASH1",
    )
    job2 = _insert_job(
        db_path, app_id, media_file_id="f2",
        source_path=ep2, target_path=tmp_path / "target" / "ep2.mkv", download_id="HASH1",
    )

    arr = FakeArrApp()
    download_client = FakeDownloadClient(
        torrents=[Torrent(id=9, name="Show S01", hash="hash1", percent_done=1.0, status=0, download_dir="/d")]
    )

    with session(db_path) as conn:
        run_once(
            conn, app_id=app_id, arr_client=arr, download_client=download_client,
            sidecar_extensions=[], verify_mode="size", settle_window_seconds=0,
        )

    assert _state(db_path, job1) == "RECONCILED"  # finished its own steps...
    assert _state(db_path, job2) == "NEEDS_ATTENTION"  # ...but its sibling didn't
    assert download_client.removed == []  # so the torrent must not be touched


def test_removal_gate_removes_torrent_once_open(db_path, tmp_path):
    app_id = _insert_app(db_path)
    staging = tmp_path / "staging" / "Movie2024"
    staging.mkdir(parents=True)
    source = staging / "movie.mkv"
    source.write_bytes(b"data")

    job_id = _insert_job(
        db_path, app_id, media_file_id="f1",
        source_path=source, target_path=tmp_path / "target" / "movie.mkv", download_id="HASH1",
    )

    arr = FakeArrApp(queue=[])  # nothing in the *arr's queue -> "gone", gate condition (b) satisfied
    download_client = FakeDownloadClient(
        torrents=[Torrent(id=9, name="Movie", hash="hash1", percent_done=1.0, status=0, download_dir="/d")]
    )

    with session(db_path) as conn:
        run_once(
            conn, app_id=app_id, arr_client=arr, download_client=download_client,
            sidecar_extensions=[], verify_mode="size", settle_window_seconds=0,
        )

    assert _state(db_path, job_id) == "DONE"
    assert download_client.removed == [(9, True)]


def test_removal_gate_withholds_while_queue_entry_still_present(db_path, tmp_path):
    app_id = _insert_app(db_path)
    staging = tmp_path / "staging" / "Movie2024"
    staging.mkdir(parents=True)
    source = staging / "movie.mkv"
    source.write_bytes(b"data")

    job_id = _insert_job(
        db_path, app_id, media_file_id="f1",
        source_path=source, target_path=tmp_path / "target" / "movie.mkv", download_id="HASH1",
    )

    arr = FakeArrApp(
        queue=[QueueEntry(download_id="hash1", tracked_download_state="downloading", tracked_download_status="ok")]
    )
    download_client = FakeDownloadClient(
        torrents=[Torrent(id=9, name="Movie", hash="hash1", percent_done=1.0, status=0, download_dir="/d")]
    )

    with session(db_path) as conn:
        run_once(
            conn, app_id=app_id, arr_client=arr, download_client=download_client,
            sidecar_extensions=[], verify_mode="size", settle_window_seconds=0,
        )

    assert _state(db_path, job_id) == "RECONCILED"  # not CLEANED
    assert download_client.removed == []
