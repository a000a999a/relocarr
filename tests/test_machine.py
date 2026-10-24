from pathlib import Path

import pytest

from relocarr.db import init_db, session
from relocarr.jobs.machine import advance_clean, advance_copy, advance_reconcile, compute_backoff_minutes

from .fakes import FakeArrApp


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


def _insert_job(db_path, app_id, *, source_path, target_path, size_bytes=4, download_id=None, state="IMPORTED") -> int:
    with session(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO jobs (app_id, download_id, media_file_id, unmonitor_id, source_path, target_path, size_bytes, state)
               VALUES (?, ?, 'file-1', 42, ?, ?, ?, ?)""",
            (app_id, download_id, str(source_path), str(target_path), size_bytes, state),
        )
        return cur.lastrowid


def _get_job(db_path, job_id):
    with session(db_path) as conn:
        return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def test_advance_copy_reaches_published_on_success(db_path, tmp_path):
    app_id = _insert_app(db_path)
    source = tmp_path / "staging" / "movie.mkv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"fake movie data")
    target = tmp_path / "target" / "movie.mkv"
    job_id = _insert_job(db_path, app_id, source_path=source, target_path=target, size_bytes=source.stat().st_size)

    with session(db_path) as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        advance_copy(conn, job, sidecar_extensions=["srt"], verify_mode="checksum")

    job = _get_job(db_path, job_id)
    assert job["state"] == "PUBLISHED"
    assert target.read_bytes() == b"fake movie data"


def test_advance_copy_also_copies_matching_sidecar(db_path, tmp_path):
    app_id = _insert_app(db_path)
    source = tmp_path / "staging" / "movie.mkv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"data")
    (source.parent / "movie.srt").write_text("subtitle")
    target = tmp_path / "target" / "movie.mkv"
    job_id = _insert_job(db_path, app_id, source_path=source, target_path=target)

    with session(db_path) as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        advance_copy(conn, job, sidecar_extensions=["srt"], verify_mode="size")

    assert (tmp_path / "target" / "movie.srt").read_text() == "subtitle"


def test_advance_copy_permanent_failure_sets_needs_attention(db_path, tmp_path):
    app_id = _insert_app(db_path)
    source = tmp_path / "staging" / "does-not-exist.mkv"  # never created
    target = tmp_path / "target" / "does-not-exist.mkv"
    job_id = _insert_job(db_path, app_id, source_path=source, target_path=target)

    with session(db_path) as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        advance_copy(conn, job, sidecar_extensions=["srt"], verify_mode="size")

    job = _get_job(db_path, job_id)
    assert job["state"] == "NEEDS_ATTENTION"
    assert job["error"]


def test_advance_reconcile_calls_arr_client_with_correct_ids(db_path, tmp_path):
    app_id = _insert_app(db_path)
    job_id = _insert_job(
        db_path, app_id, source_path=tmp_path / "a", target_path=tmp_path / "b", state="PUBLISHED"
    )
    arr = FakeArrApp()

    with session(db_path) as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        advance_reconcile(conn, job, arr)

    assert arr.deleted_files == ["file-1"]
    assert arr.unmonitored == [42]
    assert _get_job(db_path, job_id)["state"] == "RECONCILED"


def test_advance_reconcile_failure_sets_needs_attention(db_path, tmp_path):
    app_id = _insert_app(db_path)
    job_id = _insert_job(
        db_path, app_id, source_path=tmp_path / "a", target_path=tmp_path / "b", state="PUBLISHED"
    )
    arr = FakeArrApp()
    arr.fail_delete = True

    with session(db_path) as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        advance_reconcile(conn, job, arr)

    assert _get_job(db_path, job_id)["state"] == "NEEDS_ATTENTION"


def test_advance_clean_prunes_empty_folder_and_marks_done(db_path, tmp_path):
    app_id = _insert_app(db_path)
    movie_folder = tmp_path / "staging" / "Movie2024"
    movie_folder.mkdir(parents=True)  # file already deleted by Radarr's own DELETE call (spike 8)
    source = movie_folder / "movie.mkv"
    job_id = _insert_job(
        db_path, app_id, source_path=source, target_path=tmp_path / "t", state="RECONCILED"
    )

    with session(db_path) as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        advance_clean(conn, job)

    assert not movie_folder.exists()
    assert _get_job(db_path, job_id)["state"] == "DONE"


def test_advance_clean_leaves_nonempty_folder(db_path, tmp_path):
    app_id = _insert_app(db_path)
    movie_folder = tmp_path / "staging" / "Movie2024"
    movie_folder.mkdir(parents=True)
    (movie_folder / "still-here.txt").touch()  # e.g. a sibling job's file
    source = movie_folder / "movie.mkv"
    job_id = _insert_job(
        db_path, app_id, source_path=source, target_path=tmp_path / "t", state="RECONCILED"
    )

    with session(db_path) as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        advance_clean(conn, job)

    assert movie_folder.exists()  # not deleted -- not actually empty
    assert _get_job(db_path, job_id)["state"] == "DONE"


def test_advance_copy_dry_run_performs_no_action(db_path, tmp_path):
    app_id = _insert_app(db_path)
    source = tmp_path / "staging" / "movie.mkv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"data")
    target = tmp_path / "target" / "movie.mkv"
    job_id = _insert_job(db_path, app_id, source_path=source, target_path=target)

    with session(db_path) as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        advance_copy(conn, job, sidecar_extensions=["srt"], verify_mode="size", dry_run=True)

    assert not target.exists()  # no file ever written to the destination
    assert _get_job(db_path, job_id)["state"] == "IMPORTED"  # state untouched


def test_advance_reconcile_dry_run_performs_no_action(db_path, tmp_path):
    app_id = _insert_app(db_path)
    job_id = _insert_job(
        db_path, app_id, source_path=tmp_path / "a", target_path=tmp_path / "b", state="PUBLISHED"
    )
    arr = FakeArrApp()

    with session(db_path) as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        advance_reconcile(conn, job, arr, dry_run=True)

    assert arr.deleted_files == []
    assert arr.unmonitored == []
    assert _get_job(db_path, job_id)["state"] == "PUBLISHED"


def test_advance_clean_dry_run_leaves_folder_and_state(db_path, tmp_path):
    app_id = _insert_app(db_path)
    movie_folder = tmp_path / "staging" / "Movie2024"
    movie_folder.mkdir(parents=True)  # already empty, WOULD be pruned for real
    source = movie_folder / "movie.mkv"
    job_id = _insert_job(
        db_path, app_id, source_path=source, target_path=tmp_path / "t", state="RECONCILED"
    )

    with session(db_path) as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        advance_clean(conn, job, dry_run=True)

    assert movie_folder.exists()
    assert _get_job(db_path, job_id)["state"] == "RECONCILED"


def test_backoff_doubles_and_caps(db_path):
    assert compute_backoff_minutes(1) == 2
    assert compute_backoff_minutes(2) == 4
    assert compute_backoff_minutes(3) == 8
    assert compute_backoff_minutes(4) == 16
    assert compute_backoff_minutes(5) == 30  # 32 capped to 30
    assert compute_backoff_minutes(10) == 30  # stays capped


def test_advance_copy_schedules_backoff_when_destination_unhealthy(db_path, tmp_path):
    app_id = _insert_app(db_path)
    source = tmp_path / "staging" / "movie.mkv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"data")

    # Target's parent path component is actually a FILE, not a directory --
    # mkdir(parents=True) must fail deterministically, simulating a dead
    # destination without relying on permission bits (which root bypasses).
    blocker = tmp_path / "target_is_a_file"
    blocker.write_bytes(b"not a directory")
    target = blocker / "sub" / "movie.mkv"

    job_id = _insert_job(db_path, app_id, source_path=source, target_path=target)

    with session(db_path) as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        advance_copy(conn, job, sidecar_extensions=[], verify_mode="size")

    job = _get_job(db_path, job_id)
    assert job["state"] == "COPYING"  # stays in COPYING, not NEEDS_ATTENTION -- this is transient
    assert job["retry_count"] == 1
    assert job["error"]

    from datetime import datetime, timezone

    next_attempt = datetime.strptime(job["next_attempt_at"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc
    )
    assert next_attempt > datetime.now(timezone.utc)  # scheduled into the future, ~2 min out


def test_advance_copy_resets_backoff_bookkeeping_on_success(db_path, tmp_path):
    app_id = _insert_app(db_path)
    source = tmp_path / "staging" / "movie.mkv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"data")
    target = tmp_path / "target" / "movie.mkv"
    job_id = _insert_job(db_path, app_id, source_path=source, target_path=target)

    # Simulate a job that already failed twice before this (successful) attempt.
    with session(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET retry_count = 2, next_attempt_at = '2020-01-01T00:00:00.000Z' WHERE id = ?",
            (job_id,),
        )
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        advance_copy(conn, job, sidecar_extensions=["srt"], verify_mode="size")

    job = _get_job(db_path, job_id)
    assert job["state"] == "PUBLISHED"
    assert job["retry_count"] == 0
    assert job["next_attempt_at"] is None


def test_advance_clean_removes_a_larger_duplicate_sharing_title_and_year(db_path, tmp_path):
    app_id = _insert_app(db_path)
    movie_folder = tmp_path / "staging" / "Movie2024"
    movie_folder.mkdir(parents=True)
    source = movie_folder / "movie.mkv"

    target_dir = tmp_path / "target"
    target_dir.mkdir()
    target = target_dir / "Example Feature.1982.mp4"
    target.write_bytes(b"x" * 100)  # the file this job just landed
    old = target_dir / "Example Feature (1982).mkv"
    old.write_bytes(b"x" * 999)  # an old, larger, untracked duplicate

    job_id = _insert_job(
        db_path, app_id, source_path=source, target_path=target, size_bytes=100, state="RECONCILED"
    )

    with session(db_path) as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        advance_clean(conn, job)

    assert not old.exists()
    assert target.exists()
    assert _get_job(db_path, job_id)["state"] == "DONE"


def test_advance_clean_dry_run_does_not_remove_a_larger_duplicate(db_path, tmp_path):
    app_id = _insert_app(db_path)
    movie_folder = tmp_path / "staging" / "Movie2024"
    movie_folder.mkdir(parents=True)
    source = movie_folder / "movie.mkv"

    target_dir = tmp_path / "target"
    target_dir.mkdir()
    target = target_dir / "Example Feature.1982.mp4"
    target.write_bytes(b"x" * 100)
    old = target_dir / "Example Feature (1982).mkv"
    old.write_bytes(b"x" * 999)

    job_id = _insert_job(
        db_path, app_id, source_path=source, target_path=target, size_bytes=100, state="RECONCILED"
    )

    with session(db_path) as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        advance_clean(conn, job, dry_run=True)

    assert old.exists()  # dry run -- nothing actually removed
