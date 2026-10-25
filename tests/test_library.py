import asyncio

import pytest

from relocarr.config import settings as app_settings
from relocarr.db import init_db, session
from relocarr.library import (
    MovieLibraryCache,
    find_duplicate_groups,
    parse_title_year,
    remove_larger_duplicates,
    run_library_scan_forever,
    scan_movie_files,
)


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

    files, unreachable = scan_movie_files(apps)

    names = {f["name"] for f in files}
    assert names == {"Movie One.mkv", "Movie Two.mp4"}
    sizes = {f["name"]: f["size_bytes"] for f in files}
    assert sizes["Movie One.mkv"] == 100
    assert sizes["Movie Two.mp4"] == 200
    assert unreachable == []
    assert all(isinstance(f["mtime"], float) for f in files)


def test_scan_movie_files_ignores_sonarr_apps(tmp_path):
    db_path = tmp_path / "relocarr.db"
    init_db(db_path)
    target = tmp_path / "series"
    target.mkdir()
    (target / "Episode.mkv").write_bytes(b"x" * 100)
    apps = _insert_app(db_path, "sonarr", str(target))

    assert scan_movie_files(apps) == ([], [])


def test_scan_movie_files_recurses_into_subfolders(tmp_path):
    db_path = tmp_path / "relocarr.db"
    init_db(db_path)
    target = tmp_path / "movies"
    (target / "Sub Folder").mkdir(parents=True)
    (target / "Sub Folder" / "Nested.mkv").write_bytes(b"x" * 50)
    apps = _insert_app(db_path, "radarr", str(target))

    files, _ = scan_movie_files(apps)
    assert [f["name"] for f in files] == ["Nested.mkv"]


def test_scan_movie_files_missing_target_root_is_reported_unreachable(tmp_path):
    db_path = tmp_path / "relocarr.db"
    init_db(db_path)
    missing = str(tmp_path / "does-not-exist")
    apps = _insert_app(db_path, "radarr", missing)

    files, unreachable = scan_movie_files(apps)
    assert files == []
    assert unreachable == [missing]


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

    files, _ = scan_movie_files(apps)
    assert len(files) == 1


def test_parse_title_year_extracts_title_and_year():
    assert parse_title_year("Example Feature (1982).mkv") == ("Example Feature", 1982)


def test_parse_title_year_handles_mp4():
    assert parse_title_year("Nature Documentary (2020).mp4") == ("Nature Documentary", 2020)


def test_parse_title_year_returns_none_without_a_year():
    assert parse_title_year("Some Movie.mkv") is None


def test_parse_title_year_returns_none_for_malformed_name():
    assert parse_title_year("1982.mkv") is None


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
        return [{"name": "should-never-be-seen"}], []

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
    assert cache.last_timeout_at is not None


@pytest.mark.asyncio
async def test_run_library_scan_forever_records_unreachable_roots(tmp_path, monkeypatch):
    db_path = tmp_path / "relocarr.db"
    init_db(db_path)
    _insert_app(db_path, "radarr", str(tmp_path / "offline-shield"))

    monkeypatch.setattr("relocarr.library.SCAN_INTERVAL_SECONDS", 10.0)

    cache = MovieLibraryCache()
    stop_event = asyncio.Event()
    task = asyncio.create_task(run_library_scan_forever(db_path, cache, stop_event))
    await asyncio.sleep(0.2)
    stop_event.set()
    await asyncio.wait_for(task, timeout=2)

    assert cache.unreachable_roots == [str(tmp_path / "offline-shield")]


def _file(name, size_gb, app_id=1):
    return {"name": name, "path": f"/t/{name}", "app_id": app_id, "app_name": "Radarr", "size_bytes": int(size_gb * 1024**3)}


def test_find_duplicate_groups_groups_by_title_and_year():
    files = [
        _file("Example Feature (1982).mkv", 4),
        _file("Example Feature.1982.mp4", 1),
        _file("Unrelated Movie.2020.mkv", 2),
    ]

    groups = find_duplicate_groups(files)

    assert len(groups) == 1
    assert {f["name"] for f in groups[0]} == {"Example Feature (1982).mkv", "Example Feature.1982.mp4"}


def test_find_duplicate_groups_sorts_largest_first():
    files = [_file("Example Feature.1982.mp4", 1), _file("Example Feature (1982).mkv", 4)]

    groups = find_duplicate_groups(files)

    assert [f["name"] for f in groups[0]] == ["Example Feature (1982).mkv", "Example Feature.1982.mp4"]


def test_find_duplicate_groups_ignores_a_singleton_title():
    files = [_file("Only Copy.2015.mkv", 2)]
    assert find_duplicate_groups(files) == []


def test_find_duplicate_groups_ignores_unparseable_filenames():
    files = [_file("no-year-here.mkv", 2), _file("also-no-year.mkv", 2)]
    assert find_duplicate_groups(files) == []


def test_remove_larger_duplicates_removes_an_older_oversized_sibling(tmp_path):
    kept = tmp_path / "Example Feature.1982.mp4"
    kept.write_bytes(b"x" * 100)
    old = tmp_path / "Example Feature (1982).mkv"
    old.write_bytes(b"x" * 400)

    removed = remove_larger_duplicates(kept, keep_size_bytes=100)

    assert removed == ["Example Feature (1982).mkv"]
    assert not old.exists()
    assert kept.exists()


def test_remove_larger_duplicates_never_removes_something_smaller_or_equal(tmp_path):
    kept = tmp_path / "Example Feature.1982.mp4"
    kept.write_bytes(b"x" * 400)
    smaller = tmp_path / "Example Feature (1982).mkv"
    smaller.write_bytes(b"x" * 100)
    same_size = tmp_path / "Example Feature (1982).avi"
    same_size.write_bytes(b"x" * 400)

    removed = remove_larger_duplicates(kept, keep_size_bytes=400)

    assert removed == []
    assert smaller.exists()
    assert same_size.exists()


def test_remove_larger_duplicates_ignores_a_different_title_or_year(tmp_path):
    kept = tmp_path / "Example Feature.1982.mp4"
    kept.write_bytes(b"x" * 100)
    (tmp_path / "A Different Film (1982).mkv").write_bytes(b"x" * 999)
    (tmp_path / "Example Feature (1999).mkv").write_bytes(b"x" * 999)

    removed = remove_larger_duplicates(kept, keep_size_bytes=100)

    assert removed == []


def test_remove_larger_duplicates_does_nothing_for_an_unparseable_filename(tmp_path):
    kept = tmp_path / "no-year-here.mp4"
    kept.write_bytes(b"x" * 100)
    (tmp_path / "also-no-year.mkv").write_bytes(b"x" * 999)

    assert remove_larger_duplicates(kept, keep_size_bytes=100) == []


def test_parse_title_year_handles_the_dot_format_radarr_itself_uses():
    # Confirmed live, 2026-10-24: Radarr renames a freshly grabbed file to
    # "Title.Year.ext", not "Title (Year).ext" -- both formats coexist in
    # a real library (the old files use parens, new ones use dots).
    assert parse_title_year("Example Feature.1982.mp4") == ("Example Feature", 1982)


def test_parse_title_year_dot_format_handles_a_multi_word_title():
    assert parse_title_year("A Film With Several Words.2016.mkv") == ("A Film With Several Words", 2016)
