import asyncio

import pytest

from relocarr.config import settings as app_settings
from relocarr.db import init_db, session
from relocarr.disk_health import (
    DiskSpaceCache,
    FolderUsageCache,
    app_paths,
    check_disk_space,
    compute_folder_usage,
    run_disk_space_check_forever,
    run_folder_usage_scan_forever,
)


def _insert_app(db_path, kind: str, staging_root: str, target_root: str = ""):
    with session(db_path) as conn:
        conn.execute(
            """INSERT INTO apps (kind, name, base_url, api_key, webhook_token, staging_root, target_root)
               VALUES (?, ?, 'http://x', 'key', 'token', ?, ?)""",
            (kind, kind.capitalize(), staging_root, target_root),
        )


def test_check_disk_space_reports_free_and_total_for_a_real_path(tmp_path):
    result = check_disk_space([str(tmp_path)])
    assert result[str(tmp_path)]["ok"] is True
    assert result[str(tmp_path)]["free"] > 0
    assert result[str(tmp_path)]["total"] > 0


def test_check_disk_space_reports_dev_for_a_real_path(tmp_path):
    result = check_disk_space([str(tmp_path)])
    assert "dev" in result[str(tmp_path)]


def test_check_disk_space_two_paths_on_the_same_disk_report_the_same_dev(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    result = check_disk_space([str(a), str(b)])
    assert result[str(a)]["dev"] == result[str(b)]["dev"]


def test_check_disk_space_reports_error_for_a_missing_path(tmp_path):
    missing = str(tmp_path / "does-not-exist")
    result = check_disk_space([missing])
    assert result[missing]["ok"] is False
    assert "error" in result[missing]


def test_check_disk_space_one_bad_path_does_not_stop_the_others(tmp_path):
    missing = str(tmp_path / "does-not-exist")
    result = check_disk_space([str(tmp_path), missing])
    assert result[str(tmp_path)]["ok"] is True
    assert result[missing]["ok"] is False


def test_check_disk_space_skips_blank_and_duplicate_paths(tmp_path):
    result = check_disk_space([str(tmp_path), "", str(tmp_path)])
    assert list(result.keys()) == [str(tmp_path)]


@pytest.mark.asyncio
async def test_run_disk_space_check_forever_populates_the_cache(tmp_path):
    db_path = tmp_path / "relocarr.db"
    init_db(db_path)
    staging = tmp_path / "staging"
    staging.mkdir()
    _insert_app(db_path, "radarr", str(staging))

    cache = DiskSpaceCache()
    stop_event = asyncio.Event()
    task = asyncio.create_task(run_disk_space_check_forever(db_path, cache, stop_event))
    await asyncio.sleep(0.2)
    stop_event.set()
    await asyncio.wait_for(task, timeout=2)

    assert cache.entries[str(staging)]["ok"] is True
    assert cache.checked_at is not None


@pytest.mark.asyncio
async def test_run_disk_space_check_forever_keeps_stale_snapshot_on_timeout(tmp_path, monkeypatch):
    db_path = tmp_path / "relocarr.db"
    init_db(db_path)
    _insert_app(db_path, "radarr", str(tmp_path))

    monkeypatch.setattr("relocarr.disk_health.CHECK_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr("relocarr.disk_health.CHECK_INTERVAL_SECONDS", 10.0)

    def _hang(paths):
        import time

        time.sleep(2)  # far longer than the patched 0.05s timeout
        return {"should-never-be-seen": {"ok": True, "free": 1, "total": 1}}

    monkeypatch.setattr("relocarr.disk_health.check_disk_space", _hang)

    cache = DiskSpaceCache()
    cache.entries = {"stale": {"ok": True, "free": 1, "total": 2}}
    cache.checked_at = 123.0

    stop_event = asyncio.Event()
    task = asyncio.create_task(run_disk_space_check_forever(db_path, cache, stop_event))
    await asyncio.sleep(0.2)
    stop_event.set()
    await asyncio.wait_for(task, timeout=2)

    assert cache.entries == {"stale": {"ok": True, "free": 1, "total": 2}}
    assert cache.checked_at == 123.0
    assert cache.last_timeout_at is not None


def test_app_paths_collects_staging_and_target_in_first_seen_order(tmp_path):
    db_path = tmp_path / "relocarr.db"
    init_db(db_path)
    _insert_app(db_path, "radarr", str(tmp_path / "staging1"), str(tmp_path / "target1"))
    _insert_app(db_path, "sonarr", str(tmp_path / "staging2"), "")
    with session(db_path) as conn:
        apps = conn.execute("SELECT * FROM apps").fetchall()
    assert app_paths(apps) == [
        str(tmp_path / "staging1"),
        str(tmp_path / "target1"),
        str(tmp_path / "staging2"),
    ]


def test_app_paths_skips_blank_and_duplicate_paths(tmp_path):
    db_path = tmp_path / "relocarr.db"
    init_db(db_path)
    shared = str(tmp_path / "shared")
    _insert_app(db_path, "radarr", shared, "")
    _insert_app(db_path, "sonarr", shared, "")
    with session(db_path) as conn:
        apps = conn.execute("SELECT * FROM apps").fetchall()
    assert app_paths(apps) == [shared]


def test_compute_folder_usage_sums_file_sizes_recursively(tmp_path):
    folder = tmp_path / "movies"
    (folder / "sub").mkdir(parents=True)
    (folder / "a.mkv").write_bytes(b"x" * 100)
    (folder / "sub" / "b.mkv").write_bytes(b"x" * 50)

    result = compute_folder_usage([str(folder)])

    assert result[str(folder)]["ok"] is True
    assert result[str(folder)]["used"] == 150


def test_compute_folder_usage_reports_error_for_a_missing_path(tmp_path):
    missing = str(tmp_path / "does-not-exist")
    result = compute_folder_usage([missing])
    assert result[missing]["ok"] is False
    assert "error" in result[missing]


def test_compute_folder_usage_skips_blank_and_duplicate_paths(tmp_path):
    folder = tmp_path / "movies"
    folder.mkdir()
    result = compute_folder_usage([str(folder), "", str(folder)])
    assert list(result.keys()) == [str(folder)]


@pytest.mark.asyncio
async def test_run_folder_usage_scan_forever_populates_the_cache(tmp_path):
    db_path = tmp_path / "relocarr.db"
    init_db(db_path)
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "f.mkv").write_bytes(b"x" * 42)
    _insert_app(db_path, "radarr", str(staging))

    cache = FolderUsageCache()
    stop_event = asyncio.Event()
    task = asyncio.create_task(run_folder_usage_scan_forever(db_path, cache, stop_event))
    await asyncio.sleep(0.2)
    stop_event.set()
    await asyncio.wait_for(task, timeout=2)

    assert cache.entries[str(staging)]["ok"] is True
    assert cache.entries[str(staging)]["used"] == 42
    assert cache.checked_at is not None


@pytest.mark.asyncio
async def test_run_folder_usage_scan_forever_keeps_stale_snapshot_on_timeout(tmp_path, monkeypatch):
    db_path = tmp_path / "relocarr.db"
    init_db(db_path)
    _insert_app(db_path, "radarr", str(tmp_path))

    monkeypatch.setattr("relocarr.disk_health.FOLDER_SCAN_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr("relocarr.disk_health.FOLDER_SCAN_INTERVAL_SECONDS", 10.0)

    def _hang(paths):
        import time

        time.sleep(2)  # far longer than the patched 0.05s timeout
        return {"should-never-be-seen": {"ok": True, "used": 1}}

    monkeypatch.setattr("relocarr.disk_health.compute_folder_usage", _hang)

    cache = FolderUsageCache()
    cache.entries = {"stale": {"ok": True, "used": 1}}
    cache.checked_at = 123.0

    stop_event = asyncio.Event()
    task = asyncio.create_task(run_folder_usage_scan_forever(db_path, cache, stop_event))
    await asyncio.sleep(0.2)
    stop_event.set()
    await asyncio.wait_for(task, timeout=2)

    assert cache.entries == {"stale": {"ok": True, "used": 1}}
    assert cache.checked_at == 123.0
    assert cache.last_timeout_at is not None
