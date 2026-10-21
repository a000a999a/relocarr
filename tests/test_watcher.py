import pytest

from relocarr.arr.base import QueueEntry
from relocarr.clients.base import Torrent
from relocarr.clients.watcher import seal_completed_downloads
from relocarr.db import init_db, session

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


def test_seals_a_completed_tracked_torrent(db_path):
    app_id = _insert_app(db_path)
    arr = FakeArrApp(queue=[QueueEntry("hash1", "downloading", "ok")])
    dc = FakeDownloadClient(
        torrents=[Torrent(id=9, name="Movie", hash="HASH1", percent_done=1.0, status=6, download_dir="/d")]
    )

    with session(db_path) as conn:
        sealed = seal_completed_downloads(conn, app_id=app_id, arr_client=arr, download_client=dc)

    assert sealed == ["HASH1"]
    assert dc.stopped == [9]
    with session(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM downloads WHERE app_id = ? AND download_id = 'HASH1' COLLATE NOCASE", (app_id,)
        ).fetchone()
    assert row is not None


def test_does_not_seal_incomplete_torrent(db_path):
    app_id = _insert_app(db_path)
    arr = FakeArrApp(queue=[QueueEntry("hash1", "downloading", "ok")])
    dc = FakeDownloadClient(
        torrents=[Torrent(id=9, name="Movie", hash="HASH1", percent_done=0.42, status=4, download_dir="/d")]
    )

    with session(db_path) as conn:
        sealed = seal_completed_downloads(conn, app_id=app_id, arr_client=arr, download_client=dc)

    assert sealed == []
    assert dc.stopped == []


def test_does_not_touch_a_torrent_the_arr_isnt_tracking(db_path):
    # Section 4 point 6: a complete torrent whose hash is NOT in this app's
    # *arr queue must never be stopped -- e.g. someone else's download on a
    # shared Transmission instance.
    app_id = _insert_app(db_path)
    arr = FakeArrApp(queue=[])  # nothing tracked by this app
    dc = FakeDownloadClient(
        torrents=[Torrent(id=9, name="Unrelated", hash="HASH1", percent_done=1.0, status=6, download_dir="/d")]
    )

    with session(db_path) as conn:
        sealed = seal_completed_downloads(conn, app_id=app_id, arr_client=arr, download_client=dc)

    assert sealed == []
    assert dc.stopped == []


def test_does_not_reseal_an_already_sealed_download(db_path):
    app_id = _insert_app(db_path)
    arr = FakeArrApp(queue=[QueueEntry("hash1", "downloading", "ok")])
    dc = FakeDownloadClient(
        torrents=[Torrent(id=9, name="Movie", hash="HASH1", percent_done=1.0, status=0, download_dir="/d")]
    )

    with session(db_path) as conn:
        first = seal_completed_downloads(conn, app_id=app_id, arr_client=arr, download_client=dc)
        second = seal_completed_downloads(conn, app_id=app_id, arr_client=arr, download_client=dc)

    assert first == ["HASH1"]
    assert second == []  # already sealed, must not call stop() again
    assert dc.stopped == [9]


def test_dry_run_does_not_stop_or_record_sealed(db_path):
    app_id = _insert_app(db_path)
    arr = FakeArrApp(queue=[QueueEntry("hash1", "downloading", "ok")])
    dc = FakeDownloadClient(
        torrents=[Torrent(id=9, name="Movie", hash="HASH1", percent_done=1.0, status=6, download_dir="/d")]
    )

    with session(db_path) as conn:
        sealed = seal_completed_downloads(
            conn, app_id=app_id, arr_client=arr, download_client=dc, dry_run=True
        )

    assert sealed == []
    assert dc.stopped == []
    with session(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM downloads WHERE app_id = ? AND download_id = 'HASH1' COLLATE NOCASE", (app_id,)
        ).fetchone()
    assert row is None


def test_hash_matching_is_case_insensitive(db_path):
    # spike 4 (2026-10-21): Transmission's hash is lowercase, the *arr's
    # queue downloadId is uppercase, for the same torrent.
    app_id = _insert_app(db_path)
    arr = FakeArrApp(queue=[QueueEntry("HASH1", "downloading", "ok")])  # uppercase, like Radarr's downloadId
    dc = FakeDownloadClient(
        torrents=[Torrent(id=9, name="Movie", hash="hash1", percent_done=1.0, status=6, download_dir="/d")]
    )

    with session(db_path) as conn:
        sealed = seal_completed_downloads(conn, app_id=app_id, arr_client=arr, download_client=dc)

    assert sealed == ["hash1"]
