import time

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from relocarr.config import settings as app_settings
from relocarr.db import init_db, session
from relocarr.library import cache as movie_library_cache


@pytest.fixture(autouse=True)
def _reset_movie_library_cache():
    # The cache is a module-level singleton shared by the real background
    # scan loop -- reset it around every test so one test's fake library
    # contents can't leak into another's assertions.
    movie_library_cache.files = []
    movie_library_cache.scanned_at = None
    yield
    movie_library_cache.files = []
    movie_library_cache.scanned_at = None


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "config_dir", tmp_path)
    init_db(app_settings.db_path)
    from relocarr.main import app

    return TestClient(app)


def _insert_app(kind: str, target_root: str, base_url: str = "http://x") -> int:
    with session(app_settings.db_path) as conn:
        return conn.execute(
            """INSERT INTO apps (kind, name, base_url, api_key, webhook_token, staging_root, target_root)
               VALUES (?, ?, ?, 'key', 'token', '/s', ?)""",
            (kind, kind.capitalize(), base_url, target_root),
        ).lastrowid


_job_counter = iter(range(1, 1000))


def _insert_job(app_id: int, size_bytes: int, state: str = "DONE") -> int:
    media_file_id = f"f{next(_job_counter)}"
    with session(app_settings.db_path) as conn:
        return conn.execute(
            """INSERT INTO jobs (app_id, download_id, media_file_id, unmonitor_id, source_path, target_path, size_bytes, state)
               VALUES (?, NULL, ?, 1, '/s/a.mkv', '/t/a.mkv', ?, ?)""",
            (app_id, media_file_id, size_bytes, state),
        ).lastrowid


def test_log_empty_state(client):
    resp = client.get("/log")
    assert resp.status_code == 200
    assert "No completed transfers yet." in resp.text


def test_log_size_breakdown_shows_scanning_message_before_first_scan(client):
    resp = client.get("/log")
    assert resp.status_code == 200
    assert "scanning the movie folder for the first time" in resp.text


def test_log_size_breakdown_counts_come_from_the_scanned_library_not_job_history(client):
    # Job history has one small transfer; the "library" (the cache a
    # background scan would populate) has the real, much larger set of
    # files actually sitting in the movie folder. Counts must reflect the
    # library scan, not the job log.
    app_id = _insert_app("radarr", "/t")
    _insert_job(app_id, int(1 * 1024**3))  # unrelated to the breakdown now

    movie_library_cache.files = [
        {"name": "small.mkv", "app_name": "Radarr", "size_bytes": int(1 * 1024**3)},
        {"name": "two-eight.mkv", "app_name": "Radarr", "size_bytes": int(2.8 * 1024**3)},
        {"name": "three-five.mkv", "app_name": "Radarr", "size_bytes": int(3.5 * 1024**3)},
        {"name": "five.mkv", "app_name": "Radarr", "size_bytes": int(5 * 1024**3)},
    ]
    movie_library_cache.scanned_at = time.time()

    resp = client.get("/log")
    assert resp.status_code == 200
    # Non-overlapping bands now -- each file counts in exactly one bucket,
    # not every bucket its size clears.
    assert "1 file 2.5-3 GB" in resp.text
    assert "1 file 3-4 GB" in resp.text
    assert "1 file &gt; 4 GB" in resp.text


def test_log_bucket_link_includes_max_gb_for_bounded_bands(client):
    movie_library_cache.files = []
    movie_library_cache.scanned_at = time.time()

    resp = client.get("/log")
    assert resp.status_code == 200
    assert 'href="/log?min_gb=2.5&max_gb=3"' in resp.text
    assert 'href="/log?min_gb=3&max_gb=4"' in resp.text
    # The last (unbounded) bucket has no upper edge -- no &max_gb on its link.
    assert 'href="/log?min_gb=4"' in resp.text


def test_log_min_max_gb_lists_only_files_within_that_band(client):
    movie_library_cache.files = [
        {"name": "below.mkv", "app_name": "Radarr", "size_bytes": int(2 * 1024**3)},
        {"name": "in-band.mkv", "app_name": "Radarr", "size_bytes": int(3.5 * 1024**3)},
        {"name": "above.mkv", "app_name": "Radarr", "size_bytes": int(5 * 1024**3)},
    ]
    movie_library_cache.scanned_at = time.time()

    resp = client.get("/log?min_gb=3&max_gb=4")
    assert resp.status_code == 200
    assert "Movie files 3.0-4.0 GB" in resp.text
    assert "in-band.mkv" in resp.text
    assert "below.mkv" not in resp.text
    assert "above.mkv" not in resp.text  # must NOT leak into a lower band's filter


def test_log_min_gb_alone_is_still_unbounded_above(client):
    movie_library_cache.files = [
        {"name": "small.mkv", "app_name": "Radarr", "size_bytes": int(1 * 1024**3)},
        {"name": "big.mkv", "app_name": "Radarr", "size_bytes": int(5 * 1024**3)},
    ]
    movie_library_cache.scanned_at = time.time()

    resp = client.get("/log?min_gb=3")
    assert resp.status_code == 200
    assert "Movie files &gt; 3.0 GB" in resp.text
    assert "big.mkv" in resp.text
    assert "small.mkv" not in resp.text


def test_log_min_gb_with_no_matches_shows_empty_state(client):
    movie_library_cache.files = [
        {"name": "small.mkv", "app_name": "Radarr", "size_bytes": int(1 * 1024**3)},
    ]
    movie_library_cache.scanned_at = time.time()

    resp = client.get("/log?min_gb=3")
    assert resp.status_code == 200
    assert "No movie files in that size range." in resp.text


def test_log_capacity_projection_shown_for_radarr_target(client, tmp_path):
    target = tmp_path / "movies"
    target.mkdir()
    _insert_app("radarr", str(target))

    resp = client.get("/log")
    assert resp.status_code == 200
    assert "more movies at 1 GB each" in resp.text


def test_log_capacity_projection_absent_without_radarr_target(client):
    resp = client.get("/log")
    assert resp.status_code == 200
    assert "No Radarr target folder configured yet" in resp.text


def test_log_capacity_projection_ignores_sonarr(client, tmp_path):
    target = tmp_path / "series"
    target.mkdir()
    _insert_app("sonarr", str(target))

    resp = client.get("/log")
    assert resp.status_code == 200
    assert "No Radarr target folder configured yet" in resp.text


def _file(app_id, name, size_gb, path=None):
    return {
        "name": name,
        "path": path or f"/t/{name}",
        "app_id": app_id,
        "app_name": "Radarr",
        "size_bytes": int(size_gb * 1024**3),
    }


@respx.mock
def test_log_shows_replace_action_for_a_file_radarr_still_has(client, tmp_path):
    app_id = _insert_app("radarr", str(tmp_path), base_url="http://radarr-a")
    movie_library_cache.files = [_file(app_id, "Big.mkv", 5)]
    movie_library_cache.scanned_at = time.time()

    respx.get("http://radarr-a/api/v3/movie").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 42,
                    "title": "Big Movie",
                    "year": 2020,
                    "qualityProfileId": 1,
                    "monitored": True,
                    "movieFile": {"id": 99, "path": "/t/Big.mkv"},
                }
            ],
        )
    )
    respx.get("http://radarr-a/api/v3/qualityprofile").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "name": "HD-1080p"}, {"id": 2, "name": "Small"}])
    )

    resp = client.get("/log?min_gb=4")
    assert resp.status_code == 200
    assert 'action="/log/redownload"' in resp.text
    assert ">Redownload" in resp.text


@respx.mock
def test_log_shows_remonitor_action_for_a_movie_radarr_lost_track_of(client, tmp_path):
    # This is relocarr's own normal end state for an already-processed
    # movie: unmonitored, no file, but still a known title/year in Radarr.
    app_id = _insert_app("radarr", str(tmp_path), base_url="http://radarr-b")
    movie_library_cache.files = [_file(app_id, "Example Feature (1982).mkv", 5)]
    movie_library_cache.scanned_at = time.time()

    respx.get("http://radarr-b/api/v3/movie").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 7,
                    "title": "Example Feature",
                    "year": 1982,
                    "qualityProfileId": 1,
                    "monitored": False,
                }
            ],
        )
    )
    respx.get("http://radarr-b/api/v3/qualityprofile").mock(return_value=httpx.Response(200, json=[]))

    resp = client.get("/log?min_gb=4")
    assert resp.status_code == 200
    assert "Re-monitor" in resp.text


@respx.mock
def test_log_shows_add_action_for_a_file_radarr_never_tracked(client, tmp_path):
    app_id = _insert_app("radarr", str(tmp_path), base_url="http://radarr-c")
    movie_library_cache.files = [_file(app_id, "Some Untracked Movie (2015).mkv", 5)]
    movie_library_cache.scanned_at = time.time()

    respx.get("http://radarr-c/api/v3/movie").mock(return_value=httpx.Response(200, json=[]))
    respx.get("http://radarr-c/api/v3/qualityprofile").mock(return_value=httpx.Response(200, json=[]))

    resp = client.get("/log?min_gb=4")
    assert resp.status_code == 200
    assert "Add" in resp.text
    assert "choose a profile" in resp.text


@respx.mock
def test_log_shows_unknown_for_an_unparseable_filename(client, tmp_path):
    app_id = _insert_app("radarr", str(tmp_path), base_url="http://radarr-d")
    movie_library_cache.files = [_file(app_id, "no-year-here.mkv", 5)]
    movie_library_cache.scanned_at = time.time()

    respx.get("http://radarr-d/api/v3/movie").mock(return_value=httpx.Response(200, json=[]))
    respx.get("http://radarr-d/api/v3/qualityprofile").mock(return_value=httpx.Response(200, json=[]))

    resp = client.get("/log?min_gb=4")
    assert resp.status_code == 200
    assert "can't parse title/year from filename" in resp.text


@respx.mock
def test_redownload_replace_deletes_and_searches_then_shows_result(client, tmp_path):
    app_id = _insert_app("radarr", str(tmp_path), base_url="http://radarr-e")

    respx.get("http://radarr-e/api/v3/movie").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 42,
                    "title": "Big Movie",
                    "year": 2020,
                    "qualityProfileId": 1,
                    "monitored": True,
                    "movieFile": {"id": 99, "path": "/t/Big.mkv"},
                }
            ],
        )
    )
    delete_route = respx.delete("http://radarr-e/api/v3/moviefile/99").mock(return_value=httpx.Response(200))
    command_route = respx.post("http://radarr-e/api/v3/command").mock(return_value=httpx.Response(201))

    resp = client.post(
        "/log/redownload",
        data={"app_id": app_id, "path": "/t/Big.mkv", "name": "Big.mkv", "min_gb": "4"},
    )
    assert resp.status_code == 200
    assert delete_route.called
    assert command_route.called
    assert "redownloaded Big Movie" in resp.text


@respx.mock
def test_redownload_remonitor_sets_monitored_and_searches_no_file_to_delete(client, tmp_path):
    app_id = _insert_app("radarr", str(tmp_path), base_url="http://radarr-f")

    respx.get("http://radarr-f/api/v3/movie").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 7,
                    "title": "Example Feature",
                    "year": 1982,
                    "qualityProfileId": 1,
                    "monitored": False,
                }
            ],
        )
    )
    respx.get("http://radarr-f/api/v3/movie/7").mock(
        return_value=httpx.Response(
            200, json={"id": 7, "title": "Example Feature", "monitored": False, "qualityProfileId": 1}
        )
    )
    put_route = respx.put("http://radarr-f/api/v3/movie/7").mock(return_value=httpx.Response(200))
    delete_route = respx.delete(url__regex=r".*/api/v3/moviefile/.*").mock(return_value=httpx.Response(200))
    command_route = respx.post("http://radarr-f/api/v3/command").mock(return_value=httpx.Response(201))

    resp = client.post(
        "/log/redownload",
        data={
            "app_id": app_id,
            "path": "/t/Example Feature (1982).mkv",
            "name": "Example Feature (1982).mkv",
            "min_gb": "4",
        },
    )
    assert resp.status_code == 200
    assert not delete_route.called  # nothing to delete -- Radarr already has no file for it
    assert put_route.called
    assert command_route.called
    assert "re-monitored and searched Example Feature" in resp.text


@respx.mock
def test_redownload_add_looks_up_and_adds_when_profile_given(client, tmp_path):
    app_id = _insert_app("radarr", str(tmp_path), base_url="http://radarr-g")

    respx.get("http://radarr-g/api/v3/movie").mock(return_value=httpx.Response(200, json=[]))
    respx.get("http://radarr-g/api/v3/movie/lookup").mock(
        return_value=httpx.Response(
            200, json=[{"title": "Some Untracked Movie", "year": 2015, "tmdbId": 999}]
        )
    )
    add_route = respx.post("http://radarr-g/api/v3/movie").mock(
        return_value=httpx.Response(
            201, json={"id": 88, "title": "Some Untracked Movie", "qualityProfileId": 6, "year": 2015}
        )
    )

    resp = client.post(
        "/log/redownload",
        data={
            "app_id": app_id,
            "path": "/t/Some Untracked Movie (2015).mkv",
            "name": "Some Untracked Movie (2015).mkv",
            "quality_profile_id": "6",
            "min_gb": "4",
        },
    )
    assert resp.status_code == 200
    assert add_route.called
    assert "added and searched Some Untracked Movie" in resp.text


@respx.mock
def test_redownload_add_without_a_profile_shows_error(client, tmp_path):
    app_id = _insert_app("radarr", str(tmp_path), base_url="http://radarr-h")
    respx.get("http://radarr-h/api/v3/movie").mock(return_value=httpx.Response(200, json=[]))

    resp = client.post(
        "/log/redownload",
        data={
            "app_id": app_id,
            "path": "/t/Some Untracked Movie (2015).mkv",
            "name": "Some Untracked Movie (2015).mkv",
            "min_gb": "4",
        },
    )
    assert resp.status_code == 200
    assert "needs a quality profile picked" in resp.text


@respx.mock
def test_redownload_unparseable_filename_shows_error(client, tmp_path):
    app_id = _insert_app("radarr", str(tmp_path), base_url="http://radarr-i")
    respx.get("http://radarr-i/api/v3/movie").mock(return_value=httpx.Response(200, json=[]))

    resp = client.post(
        "/log/redownload",
        data={"app_id": app_id, "path": "/t/no-year-here.mkv", "name": "no-year-here.mkv", "min_gb": "4"},
    )
    assert resp.status_code == 200
    assert "cannot tell its title/year" in resp.text


@respx.mock
def test_redownload_bulk_acts_only_on_submitted_checkboxes(client, tmp_path):
    # Simulates the Log page's "all checked by default" checkboxes: the
    # three actionable rows get submitted (unparseable-filename rows have
    # no checkbox at all, so they're never in `paths` to begin with --
    # unlike an old range-based bulk action, nothing outside the explicit
    # selection is ever touched).
    app_id = _insert_app("radarr", str(tmp_path), base_url="http://radarr-j")
    movie_library_cache.files = [
        _file(app_id, "Replace.mkv", 3.5, path="/t/Replace.mkv"),
        _file(app_id, "Example Feature (1982).mkv", 3.6),
        _file(app_id, "Untracked Movie (2015).mkv", 3.7),
    ]
    movie_library_cache.scanned_at = time.time()

    respx.get("http://radarr-j/api/v3/movie").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 1,
                    "title": "Replace",
                    "year": 2020,
                    "qualityProfileId": 1,
                    "monitored": True,
                    "movieFile": {"id": 101, "path": "/t/Replace.mkv"},
                },
                {
                    "id": 7,
                    "title": "Example Feature",
                    "year": 1982,
                    "qualityProfileId": 1,
                    "monitored": False,
                },
            ],
        )
    )
    respx.get("http://radarr-j/api/v3/movie/7").mock(
        return_value=httpx.Response(200, json={"id": 7, "monitored": False, "qualityProfileId": 1, "title": "X"})
    )
    respx.put("http://radarr-j/api/v3/movie/7").mock(return_value=httpx.Response(200))
    respx.delete(url__regex=r".*/api/v3/moviefile/.*").mock(return_value=httpx.Response(200))
    respx.post("http://radarr-j/api/v3/command").mock(return_value=httpx.Response(201))
    # The POST redirects back to the same filtered /log view, which
    # re-enriches the list against Radarr -- mock that too.
    respx.get("http://radarr-j/api/v3/qualityprofile").mock(return_value=httpx.Response(200, json=[]))

    # No quality_profile_id submitted, so "Untracked Movie" (an "add" case)
    # can't proceed -- counted as skipped, not fatal to the other two.
    resp = client.post(
        "/log/redownload-bulk",
        data={
            "paths": [
                f"{app_id}|/t/Replace.mkv",
                f"{app_id}|/t/Example Feature (1982).mkv",
                f"{app_id}|/t/Untracked Movie (2015).mkv",
            ],
            "min_gb": "3",
            "max_gb": "4",
        },
    )
    assert resp.status_code == 200
    assert "Redownload triggered for 2 files" in resp.text
    assert "1 skipped" in resp.text


@respx.mock
def test_redownload_bulk_unchecking_a_box_leaves_that_file_untouched(client, tmp_path):
    app_id = _insert_app("radarr", str(tmp_path), base_url="http://radarr-k")
    movie_library_cache.files = [
        _file(app_id, "Keep.mkv", 3.5, path="/t/Keep.mkv"),
        _file(app_id, "Unchecked.mkv", 3.6, path="/t/Unchecked.mkv"),
    ]
    movie_library_cache.scanned_at = time.time()

    respx.get("http://radarr-k/api/v3/movie").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 1,
                    "title": "Keep",
                    "year": 2020,
                    "qualityProfileId": 1,
                    "monitored": True,
                    "movieFile": {"id": 101, "path": "/t/Keep.mkv"},
                },
                {
                    "id": 2,
                    "title": "Unchecked",
                    "year": 2021,
                    "qualityProfileId": 1,
                    "monitored": True,
                    "movieFile": {"id": 102, "path": "/t/Unchecked.mkv"},
                },
            ],
        )
    )
    delete_route = respx.delete(url__regex=r".*/api/v3/moviefile/.*").mock(return_value=httpx.Response(200))
    respx.post("http://radarr-k/api/v3/command").mock(return_value=httpx.Response(201))

    # Only "Keep.mkv"'s checkbox is submitted -- as if the user unchecked
    # "Unchecked.mkv" before clicking the bulk button.
    resp = client.post(
        "/log/redownload-bulk",
        data={"paths": [f"{app_id}|/t/Keep.mkv"]},
    )
    assert resp.status_code == 200
    assert "Redownload triggered for 1 file" in resp.text
    assert delete_route.call_count == 1
    assert delete_route.calls.last.request.url.path == "/api/v3/moviefile/101"
