import json
import time

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from relocarr.config import settings as app_settings
from relocarr.db import init_db, session
from relocarr.disk_health import cache as disk_space_cache
from relocarr.library import cache as movie_library_cache
from relocarr.plex import cache as plex_quality_cache


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


def _set_min_redownload_reduction_pct(app_id: int, pct: int) -> None:
    with session(app_settings.db_path) as conn:
        conn.execute("UPDATE apps SET min_redownload_reduction_pct = ? WHERE id = ?", (pct, app_id))


_job_counter = iter(range(1, 1000))


def _insert_job(app_id: int, size_bytes: int, state: str = "DONE") -> int:
    media_file_id = f"f{next(_job_counter)}"
    with session(app_settings.db_path) as conn:
        return conn.execute(
            """INSERT INTO jobs (app_id, download_id, media_file_id, unmonitor_id, source_path, target_path, size_bytes, state)
               VALUES (?, NULL, ?, 1, '/s/a.mkv', '/t/a.mkv', ?, ?)""",
            (app_id, media_file_id, size_bytes, state),
        ).lastrowid


def _insert_log_job(app_id: int, target_path: str, state: str = "DONE", error: str | None = None) -> int:
    media_file_id = f"f{next(_job_counter)}"
    with session(app_settings.db_path) as conn:
        return conn.execute(
            """INSERT INTO jobs (app_id, download_id, media_file_id, unmonitor_id, source_path, target_path, size_bytes, state, error)
               VALUES (?, NULL, ?, 1, '/s/a.mkv', ?, 1000, ?, ?)""",
            (app_id, media_file_id, target_path, state, error),
        ).lastrowid


def test_log_filter_recent_transfers_by_state(client):
    app_id = _insert_app("radarr", "/t")
    _insert_log_job(app_id, "/t/done-one.mkv", state="DONE")
    _insert_log_job(app_id, "/t/needs-one.mkv", state="NEEDS_ATTENTION")

    resp = client.get("/log?log_state=DONE")
    assert resp.status_code == 200
    assert "done-one.mkv" in resp.text
    assert "needs-one.mkv" not in resp.text


def test_log_filter_recent_transfers_by_app(client):
    app1_id = _insert_app("radarr", "/t1", base_url="http://log-app1")
    app2_id = _insert_app("radarr", "/t2", base_url="http://log-app2")
    _insert_log_job(app1_id, "/t/app1-file.mkv", state="DONE")
    _insert_log_job(app2_id, "/t/app2-file.mkv", state="DONE")

    resp = client.get(f"/log?log_app_id={app1_id}")
    assert resp.status_code == 200
    assert "app1-file.mkv" in resp.text
    assert "app2-file.mkv" not in resp.text


def test_log_filter_log_app_id_blank_means_all_apps(client):
    # Root-caused live, 2026-10-29: the filter form's "All apps" option
    # submits log_app_id="" (the unfiltered default), and FastAPI/Pydantic
    # refuses to coerce an empty string to an optional int -- 422'd on
    # every single "All apps" + another filter combination.
    app1_id = _insert_app("radarr", "/t1", base_url="http://log-app1b")
    app2_id = _insert_app("radarr", "/t2", base_url="http://log-app2b")
    _insert_log_job(app1_id, "/t/app1-blank.mkv", state="DONE")
    _insert_log_job(app2_id, "/t/app2-blank.mkv", state="DONE")

    resp = client.get("/log?log_app_id=&log_state=DONE")
    assert resp.status_code == 200
    assert "app1-blank.mkv" in resp.text
    assert "app2-blank.mkv" in resp.text


def test_log_filter_recent_transfers_error_only(client):
    app_id = _insert_app("radarr", "/t")
    _insert_log_job(app_id, "/t/clean.mkv", state="DONE", error=None)
    _insert_log_job(app_id, "/t/broken.mkv", state="NEEDS_ATTENTION", error="boom")

    resp = client.get("/log?log_error_only=true")
    assert resp.status_code == 200
    assert "broken.mkv" in resp.text
    assert "clean.mkv" not in resp.text


def test_log_filter_recent_transfers_empty_state_message(client):
    app_id = _insert_app("radarr", "/t")
    _insert_log_job(app_id, "/t/done.mkv", state="DONE")

    resp = client.get("/log?log_state=SKIPPED")
    assert resp.status_code == 200
    assert "No transfers match these filters." in resp.text


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
    # The route only ever reads disk_space_cache now -- it never calls
    # shutil.disk_usage() itself (a wedged mount must not be able to hang
    # this page; see disk_health.py), so the cache has to be pre-populated
    # here the same way the real background loop would.
    disk_space_cache.entries[str(target)] = {"free": 10 * 1024**3, "total": 20 * 1024**3, "ok": True}

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
def test_log_shows_created_date_and_plex_placeholder_when_plex_not_configured(client, tmp_path):
    # PLAN.md item 4 (2026-10-28): the poster source switched from Radarr
    # to Plex, with no fallback to Radarr's poster when Plex isn't
    # configured -- a "Plex integration needed" placeholder shows instead.
    app_id = _insert_app("radarr", str(tmp_path), base_url="http://radarr-poster")
    movie_library_cache.files = [
        {**_file(app_id, "Big.mkv", 5), "mtime": time.mktime((2026, 3, 4, 0, 0, 0, 0, 0, -1))}
    ]
    movie_library_cache.scanned_at = time.time()

    respx.get("http://radarr-poster/api/v3/movie").mock(
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
                    "images": [{"coverType": "poster", "remoteUrl": "https://image.tmdb.org/big.jpg"}],
                }
            ],
        )
    )
    respx.get("http://radarr-poster/api/v3/qualityprofile").mock(return_value=httpx.Response(200, json=[]))

    resp = client.get("/log?min_gb=4")
    assert resp.status_code == 200
    assert "2026-03-04" in resp.text
    assert "Plex integration needed" in resp.text
    assert 'src="https://image.tmdb.org/big.jpg"' not in resp.text  # no fallback to Radarr's poster


@respx.mock
def test_log_shows_plex_poster_and_deep_link_when_configured_and_matched(client, tmp_path):
    app_id = _insert_app("radarr", str(tmp_path), base_url="http://radarr-poster2")
    movie_library_cache.files = [_file(app_id, "Big.mkv", 5)]
    movie_library_cache.scanned_at = time.time()
    respx.get("http://radarr-poster2/api/v3/movie").mock(return_value=httpx.Response(200, json=[]))
    respx.get("http://radarr-poster2/api/v3/qualityprofile").mock(return_value=httpx.Response(200, json=[]))

    plex_quality_cache.checked_at = time.time()
    plex_quality_cache.index = {"Big.mkv": {"resolution": "1080", "rating_key": 501}}
    plex_quality_cache.base_url = "http://plex:32400"
    plex_quality_cache.machine_identifier = "abc123"

    resp = client.get("/log?min_gb=4")
    assert resp.status_code == 200
    assert 'src="/plex/poster/501"' in resp.text
    assert 'href="http://plex:32400/web/index.html#!/server/abc123/details?key=/library/metadata/501"' in resp.text
    assert "Plex integration needed" not in resp.text


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
def test_redownload_replace_deletes_and_grabs_a_smaller_release(client, tmp_path):
    app_id = _insert_app("radarr", str(tmp_path), base_url="http://radarr-e")
    current_size = 5 * 1024**3

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
    respx.get("http://radarr-e/api/v3/release").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "guid": "smaller-release",
                    "indexerId": 2,
                    "size": 2 * 1024**3,
                    "title": "Smaller Release",
                    "seeders": 50,
                    "rejections": [],
                }
            ],
        )
    )
    delete_route = respx.delete("http://radarr-e/api/v3/moviefile/99").mock(return_value=httpx.Response(200))
    grab_route = respx.post("http://radarr-e/api/v3/release").mock(return_value=httpx.Response(200))

    resp = client.post(
        "/log/redownload",
        data={"app_id": app_id, "path": "/t/Big.mkv", "name": "Big.mkv", "size_bytes": current_size, "min_gb": "4"},
    )
    assert resp.status_code == 200
    assert delete_route.called
    assert grab_route.called
    assert "redownloading Big Movie" in resp.text
    assert "2.00 GB" in resp.text
    assert "was 5.00 GB" in resp.text


@respx.mock
def test_redownload_uses_the_apps_own_configured_reduction_threshold(client, tmp_path):
    # Confirmed live, 2026-10-28: the "how much smaller" threshold is
    # configurable per app (App Detail page), not the hardcoded 5%
    # default -- this is an end-to-end check that the route actually
    # reads and applies that per-app value, not just the radarr.py client
    # function in isolation.
    app_id = _insert_app("radarr", str(tmp_path), base_url="http://radarr-pct")
    _set_min_redownload_reduction_pct(app_id, 50)  # strict: must be < 50% of current size
    current_size = 10 * 1024**3

    respx.get("http://radarr-pct/api/v3/movie").mock(
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
    respx.get("http://radarr-pct/api/v3/release").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    # 90% of current -- clears the hardcoded 5% default
                    # but must be rejected under the app's strict 50%.
                    "guid": "barely-smaller",
                    "indexerId": 2,
                    "size": int(current_size * 0.90),
                    "title": "Barely Smaller",
                    "seeders": 50,
                    "rejections": [],
                }
            ],
        )
    )

    resp = client.post(
        "/log/redownload",
        data={"app_id": app_id, "path": "/t/Big.mkv", "name": "Big.mkv", "size_bytes": current_size, "min_gb": "4"},
    )
    assert resp.status_code == 200
    assert "no release smaller than the current" in resp.text


@respx.mock
def test_redownload_replace_finds_nothing_smaller_does_not_delete_or_grab(client, tmp_path):
    app_id = _insert_app("radarr", str(tmp_path), base_url="http://radarr-e2")
    current_size = 5 * 1024**3

    respx.get("http://radarr-e2/api/v3/movie").mock(
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
    respx.get("http://radarr-e2/api/v3/release").mock(return_value=httpx.Response(200, json=[]))
    delete_route = respx.delete("http://radarr-e2/api/v3/moviefile/99").mock(return_value=httpx.Response(200))

    resp = client.post(
        "/log/redownload",
        data={"app_id": app_id, "path": "/t/Big.mkv", "name": "Big.mkv", "size_bytes": current_size, "min_gb": "4"},
    )
    assert resp.status_code == 200
    assert not delete_route.called  # found nothing better -- the existing file must survive
    assert "no release smaller than the current 5.00 GB found" in resp.text


@respx.mock
def test_redownload_remonitor_grabs_smaller_release_no_file_to_delete(client, tmp_path):
    app_id = _insert_app("radarr", str(tmp_path), base_url="http://radarr-f")
    current_size = 5 * 1024**3

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
    respx.get("http://radarr-f/api/v3/release").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"guid": "g", "indexerId": 1, "size": 2 * 1024**3, "title": "Smaller", "seeders": 50, "rejections": []}
            ],
        )
    )
    delete_route = respx.delete(url__regex=r".*/api/v3/moviefile/.*").mock(return_value=httpx.Response(200))
    grab_route = respx.post("http://radarr-f/api/v3/release").mock(return_value=httpx.Response(200))

    resp = client.post(
        "/log/redownload",
        data={
            "app_id": app_id,
            "path": "/t/Example Feature (1982).mkv",
            "name": "Example Feature (1982).mkv",
            "size_bytes": current_size,
            "min_gb": "4",
        },
    )
    assert resp.status_code == 200
    assert not delete_route.called  # nothing to delete -- Radarr already has no file for it
    assert put_route.called
    assert grab_route.called
    assert "redownloading Example Feature" in resp.text


@respx.mock
def test_redownload_add_looks_up_adds_and_grabs_when_smaller_release_found(client, tmp_path):
    app_id = _insert_app("radarr", str(tmp_path), base_url="http://radarr-g")
    current_size = 5 * 1024**3

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
    respx.get("http://radarr-g/api/v3/release").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"guid": "g", "indexerId": 1, "size": 2 * 1024**3, "title": "Smaller", "seeders": 50, "rejections": []}
            ],
        )
    )
    grab_route = respx.post("http://radarr-g/api/v3/release").mock(return_value=httpx.Response(200))

    resp = client.post(
        "/log/redownload",
        data={
            "app_id": app_id,
            "path": "/t/Some Untracked Movie (2015).mkv",
            "name": "Some Untracked Movie (2015).mkv",
            "size_bytes": current_size,
            "quality_profile_id": "6",
            "min_gb": "4",
        },
    )
    assert resp.status_code == 200
    assert add_route.called
    assert grab_route.called
    sent = json.loads(add_route.calls.last.request.content)
    assert sent["addOptions"] == {"searchForMovie": False}  # relocarr does its own size-aware search
    assert "added and redownloading Some Untracked Movie" in resp.text


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
            "size_bytes": 1000,
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
        data={
            "app_id": app_id,
            "path": "/t/no-year-here.mkv",
            "name": "no-year-here.mkv",
            "size_bytes": 1000,
            "min_gb": "4",
        },
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
    respx.get("http://radarr-j/api/v3/release").mock(
        return_value=httpx.Response(
            200,
            json=[{"guid": "g", "indexerId": 1, "size": 1 * 1024**3, "title": "Smaller", "seeders": 50, "rejections": []}],
        )
    )
    respx.post("http://radarr-j/api/v3/release").mock(return_value=httpx.Response(200))
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
    respx.get("http://radarr-k/api/v3/release").mock(
        return_value=httpx.Response(
            200,
            json=[{"guid": "g", "indexerId": 1, "size": 1 * 1024**3, "title": "Smaller", "seeders": 50, "rejections": []}],
        )
    )
    respx.post("http://radarr-k/api/v3/release").mock(return_value=httpx.Response(200))

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


def test_redownload_bulk_with_nothing_checked_does_nothing(client):
    # No "paths" form field at all -- exactly what a browser sends when
    # every checkbox is left unchecked (the default now).
    resp = client.post("/log/redownload-bulk", data={})
    assert resp.status_code == 200
    assert "No files were checked" in resp.text


def test_duplicates_page_shows_plex_quality_when_configured(client):
    app_id = _insert_app("radarr", "/t")
    # Plex reports its own, completely different path for the same file
    # (it runs on the Shield itself with a local storage path, relocarr
    # sees it via the CIFS mount) -- matched by filename, not full path.
    path = "/t/Example Feature.1982.mp4"
    movie_library_cache.files = [
        _file(app_id, "Example Feature (1982).mkv", 4),
        _file(app_id, "Example Feature.1982.mp4", 1, path=path),
    ]
    plex_quality_cache.checked_at = time.time()
    plex_quality_cache.index = {
        "Example Feature.1982.mp4": {"resolution": "1080", "codec": "hevc", "bitrate": 8000}
    }
    # Once Plex is configured, grouping itself comes from Plex's own
    # duplicate listing, not the filename regex -- this page needs it set
    # too, or the two files above would never be paired up as a group.
    plex_quality_cache.duplicate_groups = [
        [
            {"name": "Example Feature (1982).mkv", "size_bytes": int(4 * 1024**3)},
            {"name": "Example Feature.1982.mp4", "size_bytes": int(1 * 1024**3)},
        ]
    ]

    resp = client.get("/log/duplicates")
    assert resp.status_code == 200
    assert "1080p" in resp.text
    assert "HEVC" in resp.text
    assert "8.0 Mbps" in resp.text


def test_duplicates_page_groups_via_plex_when_configured_even_if_filenames_would_not_match(client):
    # The real bug this guards against, 2026-10-28: two files for the same
    # movie under different naming conventions -- the (title, year) regex
    # groups fine here too, but once Plex is configured it should be
    # Plex's own match driving the grouping, not the regex, and this
    # proves that by using a Plex group the regex path would also find
    # (the point is *which* path is actually used, see the next test for
    # one the regex would miss outright).
    app_id = _insert_app("radarr", "/t")
    movie_library_cache.files = [
        _file(app_id, "Example Reissue (2001).mp4", 2),
        _file(app_id, "Example Reissue.2001.mp4", 2),
    ]
    plex_quality_cache.checked_at = time.time()
    plex_quality_cache.duplicate_groups = [
        [
            {"name": "Example Reissue (2001).mp4", "size_bytes": int(2 * 1024**3)},
            {"name": "Example Reissue.2001.mp4", "size_bytes": int(2 * 1024**3)},
        ]
    ]

    resp = client.get("/log/duplicates")
    assert resp.status_code == 200
    assert "Example Reissue (2001).mp4" in resp.text
    assert "Example Reissue.2001.mp4" in resp.text


def test_duplicates_page_falls_back_to_filename_grouping_when_plex_has_not_checked_in_yet(client):
    app_id = _insert_app("radarr", "/t")
    movie_library_cache.files = [
        _file(app_id, "Example Feature (1982).mkv", 4),
        _file(app_id, "Example Feature.1982.mp4", 1),
    ]
    # plex_quality_cache.checked_at left at its reset default (None) --
    # Plex isn't configured/reachable, so the page must still work from
    # the filename-based grouping rather than showing nothing.

    resp = client.get("/log/duplicates")
    assert resp.status_code == 200
    assert "Example Feature (1982).mkv" in resp.text
    assert "Example Feature.1982.mp4" in resp.text


def test_duplicates_page_hints_to_configure_plex_when_not_set_up(client):
    app_id = _insert_app("radarr", "/t")
    movie_library_cache.files = [
        _file(app_id, "Example Feature (1982).mkv", 4),
        _file(app_id, "Example Feature.1982.mp4", 1),
    ]

    resp = client.get("/log/duplicates")
    assert resp.status_code == 200
    assert "/settings/plex" in resp.text
    assert "Plex integration needed" in resp.text


def test_duplicates_page_shows_plex_poster_and_deep_link_when_matched(client):
    # PLAN.md item 4 (2026-10-28): the poster source switched from Radarr
    # to Plex, proxied through relocarr's own /plex/poster route rather
    # than linked to directly (keeps the Plex token out of page source).
    app_id = _insert_app("radarr", "/t")
    movie_library_cache.files = [
        _file(app_id, "Example Feature (1982).mkv", 4),
        _file(app_id, "Example Feature.1982.mp4", 1),
    ]
    plex_quality_cache.checked_at = time.time()
    plex_quality_cache.index = {
        "Example Feature (1982).mkv": {"resolution": "1080", "rating_key": 501},
        "Example Feature.1982.mp4": {"resolution": "1080", "rating_key": 501},
    }
    plex_quality_cache.duplicate_groups = [
        [
            {"name": "Example Feature (1982).mkv", "size_bytes": int(4 * 1024**3)},
            {"name": "Example Feature.1982.mp4", "size_bytes": int(1 * 1024**3)},
        ]
    ]
    plex_quality_cache.base_url = "http://plex:32400"
    plex_quality_cache.machine_identifier = "abc123"

    resp = client.get("/log/duplicates")
    assert resp.status_code == 200
    assert 'src="/plex/poster/501"' in resp.text
    assert 'href="http://plex:32400/web/index.html#!/server/abc123/details?key=/library/metadata/501"' in resp.text
    assert "Plex integration needed" not in resp.text


def test_duplicates_page_groups_files_sharing_title_and_year(client):
    app_id = _insert_app("radarr", "/t")
    movie_library_cache.files = [
        _file(app_id, "Example Feature (1982).mkv", 4),
        _file(app_id, "Example Feature.1982.mp4", 1),
        _file(app_id, "Only Copy.2015.mkv", 2),
    ]

    resp = client.get("/log/duplicates")
    assert resp.status_code == 200
    assert "Example Feature (1982).mkv" in resp.text
    assert "Example Feature.1982.mp4" in resp.text
    assert "Only Copy.2015.mkv" not in resp.text  # not a duplicate -- excluded


def test_duplicates_page_empty_state_when_nothing_duplicated(client):
    app_id = _insert_app("radarr", "/t")
    movie_library_cache.files = [_file(app_id, "Only Copy.2015.mkv", 2)]

    resp = client.get("/log/duplicates")
    assert resp.status_code == 200
    assert "No duplicate titles found in the library right now." in resp.text


@respx.mock
def test_duplicates_delete_removes_a_radarr_tracked_file_through_the_api(client, tmp_path):
    app_id = _insert_app("radarr", str(tmp_path), base_url="http://radarr-dup1")
    kept_path = str(tmp_path / "Example Feature.1982.mp4")
    (tmp_path / "Example Feature.1982.mp4").write_bytes(b"x")

    respx.get("http://radarr-dup1/api/v3/movie").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 7,
                    "title": "Example Feature",
                    "year": 1982,
                    "qualityProfileId": 1,
                    "monitored": True,
                    "movieFile": {"id": 101, "path": kept_path},
                }
            ],
        )
    )
    delete_route = respx.delete("http://radarr-dup1/api/v3/moviefile/101").mock(
        return_value=httpx.Response(200)
    )

    resp = client.post(
        "/log/duplicates/delete", data={"paths": [f"{app_id}|{kept_path}"]}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert delete_route.called
    # Deleted through Radarr's API, not a direct unlink -- the mocked
    # delete never touches the real filesystem, so the file surviving
    # confirms the API branch was taken, not the orphan branch.
    assert (tmp_path / "Example Feature.1982.mp4").exists()


@respx.mock
def test_duplicates_delete_removes_an_orphan_file_directly(client, tmp_path):
    app_id = _insert_app("radarr", str(tmp_path), base_url="http://radarr-dup2")
    orphan_path = tmp_path / "Example Feature (1982).mkv"
    orphan_path.write_bytes(b"x")

    respx.get("http://radarr-dup2/api/v3/movie").mock(return_value=httpx.Response(200, json=[]))

    resp = client.post(
        "/log/duplicates/delete", data={"paths": [f"{app_id}|{orphan_path}"]}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert not orphan_path.exists()


def test_duplicates_delete_with_nothing_checked_deletes_nothing(client):
    resp = client.post("/log/duplicates/delete", data={}, follow_redirects=False)
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]


@respx.mock
def test_duplicates_delete_removes_the_file_from_the_library_cache_immediately(client, tmp_path):
    # Confirmed live, 2026-10-28: without this, a just-deleted duplicate
    # kept showing on reload for up to the next 10-minute library-scan
    # cycle -- confusing, since the delete itself had already succeeded.
    app_id = _insert_app("radarr", str(tmp_path), base_url="http://radarr-dup3")
    orphan_path = tmp_path / "Example Feature (1982).mkv"
    orphan_path.write_bytes(b"x")
    kept_path = str(tmp_path / "Example Feature.1982.mp4")
    movie_library_cache.files = [
        _file(app_id, "Example Feature (1982).mkv", 2, path=str(orphan_path)),
        _file(app_id, "Example Feature.1982.mp4", 1, path=kept_path),
    ]

    respx.get("http://radarr-dup3/api/v3/movie").mock(return_value=httpx.Response(200, json=[]))

    client.post(
        "/log/duplicates/delete", data={"paths": [f"{app_id}|{orphan_path}"]}, follow_redirects=False
    )

    assert [f["path"] for f in movie_library_cache.files] == [kept_path]


@respx.mock
def test_duplicates_delete_removes_the_file_from_the_plex_cache_immediately(client, tmp_path):
    app_id = _insert_app("radarr", str(tmp_path), base_url="http://radarr-dup4")
    orphan_path = tmp_path / "Example Feature (1982).mkv"
    orphan_path.write_bytes(b"x")
    plex_quality_cache.checked_at = time.time()
    plex_quality_cache.index = {"Example Feature (1982).mkv": {"resolution": "1080"}}
    plex_quality_cache.duplicate_groups = [
        [
            {"name": "Example Feature (1982).mkv", "size_bytes": int(2 * 1024**3)},
            {"name": "Example Feature.1982.mp4", "size_bytes": int(1 * 1024**3)},
        ]
    ]

    respx.get("http://radarr-dup4/api/v3/movie").mock(return_value=httpx.Response(200, json=[]))

    client.post(
        "/log/duplicates/delete", data={"paths": [f"{app_id}|{orphan_path}"]}, follow_redirects=False
    )

    assert "Example Feature (1982).mkv" not in plex_quality_cache.index
    # The group dropped to a single remaining file -- no longer a duplicate.
    assert plex_quality_cache.duplicate_groups == []


@respx.mock
def test_duplicates_delete_triggers_a_plex_library_refresh_when_plex_is_configured(client, tmp_path):
    app_id = _insert_app("radarr", str(tmp_path), base_url="http://radarr-dup5")
    orphan_path = tmp_path / "Example Feature (1982).mkv"
    orphan_path.write_bytes(b"x")
    with session(app_settings.db_path) as conn:
        conn.execute("UPDATE settings SET value = 'http://plex-dup' WHERE key = 'plex_base_url'")
        conn.execute("UPDATE settings SET value = 'tok' WHERE key = 'plex_token'")

    respx.get("http://radarr-dup5/api/v3/movie").mock(return_value=httpx.Response(200, json=[]))
    respx.get("http://plex-dup/library/sections").mock(
        return_value=httpx.Response(
            200, json={"MediaContainer": {"Directory": [{"key": "1", "type": "movie"}]}}
        )
    )
    refresh_route = respx.post("http://plex-dup/library/sections/1/refresh").mock(
        return_value=httpx.Response(200)
    )

    client.post(
        "/log/duplicates/delete", data={"paths": [f"{app_id}|{orphan_path}"]}, follow_redirects=False
    )

    assert refresh_route.called


@respx.mock
def test_duplicates_delete_does_not_attempt_a_plex_refresh_when_plex_is_not_configured(client, tmp_path):
    app_id = _insert_app("radarr", str(tmp_path), base_url="http://radarr-dup6")
    orphan_path = tmp_path / "Example Feature (1982).mkv"
    orphan_path.write_bytes(b"x")

    respx.get("http://radarr-dup6/api/v3/movie").mock(return_value=httpx.Response(200, json=[]))

    resp = client.post(
        "/log/duplicates/delete", data={"paths": [f"{app_id}|{orphan_path}"]}, follow_redirects=False
    )

    assert resp.status_code == 303
    assert not orphan_path.exists()


def test_plex_poster_route_returns_404_when_plex_not_configured(client):
    resp = client.get("/plex/poster/501")
    assert resp.status_code == 404


@respx.mock
def test_plex_poster_route_proxies_the_resized_image_from_plex(client):
    with session(app_settings.db_path) as conn:
        conn.execute("UPDATE settings SET value = 'http://plex:32400' WHERE key = 'plex_base_url'")
        conn.execute("UPDATE settings SET value = 'tok' WHERE key = 'plex_token'")

    respx.get("http://plex:32400/photo/:/transcode").mock(
        return_value=httpx.Response(200, content=b"fake-jpeg-bytes", headers={"content-type": "image/jpeg"})
    )

    resp = client.get("/plex/poster/501")

    assert resp.status_code == 200
    assert resp.content == b"fake-jpeg-bytes"
    assert resp.headers["content-type"] == "image/jpeg"
    assert "max-age" in resp.headers["cache-control"]


@respx.mock
def test_plex_poster_route_returns_502_when_plex_is_unreachable(client):
    with session(app_settings.db_path) as conn:
        conn.execute("UPDATE settings SET value = 'http://plex:32400' WHERE key = 'plex_base_url'")
        conn.execute("UPDATE settings SET value = 'tok' WHERE key = 'plex_token'")

    respx.get("http://plex:32400/photo/:/transcode").mock(return_value=httpx.Response(401))

    resp = client.get("/plex/poster/501")

    assert resp.status_code == 502
