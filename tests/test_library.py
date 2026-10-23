from relocarr.config import settings as app_settings
from relocarr.db import init_db, session
from relocarr.library import scan_movie_files


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
