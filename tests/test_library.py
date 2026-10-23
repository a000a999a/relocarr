from relocarr.config import settings as app_settings
from relocarr.db import init_db, session
from relocarr.library import parse_title_year, scan_movie_files


def _insert_app(db_path, kind: str, target_root: str, name: str | None = None):
    with session(db_path) as conn:
        conn.execute(
            """INSERT INTO apps (kind, name, base_url, api_key, webhook_token, staging_root, target_root)
               VALUES (?, ?, 'http://x', 'key', 'token', '/s', ?)""",
            (kind, name or kind.capitalize(), target_root),
        )
        return conn.execute("SELECT * FROM apps").fetchall()


def test_scan_movie_files_finds_video_files_in_radarr_target(tmp_path):
    db_path = tmp_path / "relocarr.db"
    init_db(db_path)
    target = tmp_path / "movies"
    target.mkdir()
    (target / "Movie One.mkv").write_bytes(b"x" * 100)
    (target / "Movie Two.mp4").write_bytes(b"x" * 200)
    (target / "poster.jpg").write_bytes(b"x" * 10)  # not a video extension -- ignored
    apps = _insert_app(db_path, "radarr", str(target))

    files = scan_movie_files(apps)

    names = {f["name"] for f in files}
    assert names == {"Movie One.mkv", "Movie Two.mp4"}
    sizes = {f["name"]: f["size_bytes"] for f in files}
    assert sizes["Movie One.mkv"] == 100
    assert sizes["Movie Two.mp4"] == 200


def test_scan_movie_files_ignores_sonarr_apps(tmp_path):
    db_path = tmp_path / "relocarr.db"
    init_db(db_path)
    target = tmp_path / "series"
    target.mkdir()
    (target / "Episode.mkv").write_bytes(b"x" * 100)
    apps = _insert_app(db_path, "sonarr", str(target))

    assert scan_movie_files(apps) == []


def test_scan_movie_files_recurses_into_subfolders(tmp_path):
    db_path = tmp_path / "relocarr.db"
    init_db(db_path)
    target = tmp_path / "movies"
    (target / "Sub Folder").mkdir(parents=True)
    (target / "Sub Folder" / "Nested.mkv").write_bytes(b"x" * 50)
    apps = _insert_app(db_path, "radarr", str(target))

    files = scan_movie_files(apps)
    assert [f["name"] for f in files] == ["Nested.mkv"]


def test_scan_movie_files_missing_target_root_is_skipped_not_an_error(tmp_path):
    db_path = tmp_path / "relocarr.db"
    init_db(db_path)
    apps = _insert_app(db_path, "radarr", str(tmp_path / "does-not-exist"))

    assert scan_movie_files(apps) == []


def test_scan_movie_files_dedupes_apps_sharing_a_target_root(tmp_path):
    db_path = tmp_path / "relocarr.db"
    init_db(db_path)
    target = tmp_path / "movies"
    target.mkdir()
    (target / "Movie.mkv").write_bytes(b"x" * 100)
    with session(db_path) as conn:
        conn.execute(
            """INSERT INTO apps (kind, name, base_url, api_key, webhook_token, staging_root, target_root)
               VALUES ('radarr', 'Radarr A', 'http://x', 'key', 'token-a', '/s', ?)""",
            (str(target),),
        )
        conn.execute(
            """INSERT INTO apps (kind, name, base_url, api_key, webhook_token, staging_root, target_root)
               VALUES ('radarr', 'Radarr B', 'http://y', 'key', 'token-b', '/s2', ?)""",
            (str(target),),
        )
        apps = conn.execute("SELECT * FROM apps").fetchall()

    files = scan_movie_files(apps)
    assert len(files) == 1


def test_parse_title_year_extracts_title_and_year():
    assert parse_title_year("Example Feature (1982).mkv") == ("Example Feature", 1982)


def test_parse_title_year_handles_mp4():
    assert parse_title_year("Nature Documentary (2020).mp4") == ("Nature Documentary", 2020)


def test_parse_title_year_returns_none_without_a_year():
    assert parse_title_year("Some Movie.mkv") is None


def test_parse_title_year_returns_none_for_malformed_name():
    assert parse_title_year("1982.mkv") is None


import asyncio

import pytest

from relocarr.library import MovieLibraryCache, run_library_scan_forever


@pytest.mark.asyncio
async def test_run_library_scan_forever_keeps_stale_snapshot_on_timeout(tmp_path, monkeypatch):
    db_path = tmp_path / "relocarr.db"
    init_db(db_path)

    # A scan that hangs well past the (patched, short) timeout -- simulates
    # a wedged mount: ping works, I/O never returns.
    monkeypatch.setattr("relocarr.library.SCAN_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr("relocarr.library.SCAN_INTERVAL_SECONDS", 10.0)

    def _hang(apps):
        import time

        time.sleep(2)  # far longer than the patched 0.05s timeout
        return [{"name": "should-never-be-seen"}]

    monkeypatch.setattr("relocarr.library.scan_movie_files", _hang)

    cache = MovieLibraryCache()
    cache.files = [{"name": "stale-but-good"}]
    cache.scanned_at = 123.0

    stop_event = asyncio.Event()
    task = asyncio.create_task(run_library_scan_forever(db_path, cache, stop_event))
    await asyncio.sleep(0.2)  # let the timed-out cycle happen, then settle into the 10s wait
    stop_event.set()
    await asyncio.wait_for(task, timeout=2)

    # The hung scan never got to overwrite the cache -- the old, good
    # snapshot is still there rather than being wiped or left half-written.
    assert cache.files == [{"name": "stale-but-good"}]
    assert cache.scanned_at == 123.0
