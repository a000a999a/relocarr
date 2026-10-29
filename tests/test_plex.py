import asyncio

import httpx
import pytest
import respx

from relocarr.config import settings as app_settings
from relocarr.db import init_db, session
from relocarr.plex import (
    PlexClient,
    PlexQualityCache,
    build_plex_web_url,
    run_plex_quality_check_forever,
)


@respx.mock
def test_test_connection_ok():
    client = PlexClient("http://plex:32400", "tok")
    respx.get("http://plex:32400/library/sections").mock(
        return_value=httpx.Response(200, json={"MediaContainer": {"version": "1.40.0"}})
    )

    result = client.test_connection()
    client.close()

    assert result.ok is True
    assert result.version == "1.40.0"


@respx.mock
def test_test_connection_failure():
    client = PlexClient("http://plex:32400", "tok")
    respx.get("http://plex:32400/library/sections").mock(return_value=httpx.Response(401))

    result = client.test_connection()
    client.close()

    assert result.ok is False
    assert result.error


@respx.mock
def test_build_quality_index_only_scans_movie_sections():
    client = PlexClient("http://plex:32400", "tok")
    respx.get("http://plex:32400/library/sections").mock(
        return_value=httpx.Response(
            200,
            json={
                "MediaContainer": {
                    "Directory": [
                        {"key": "1", "type": "movie", "title": "Movies"},
                        {"key": "2", "type": "show", "title": "TV Shows"},
                    ]
                }
            },
        )
    )
    movies_route = respx.get("http://plex:32400/library/sections/1/all").mock(
        return_value=httpx.Response(
            200,
            json={
                "MediaContainer": {
                    "Metadata": [
                        {
                            "title": "Example Feature",
                            "year": 1982,
                            "ratingKey": 501,
                            "Media": [
                                {
                                    "videoResolution": "1080",
                                    "videoCodec": "hevc",
                                    "bitrate": 8000,
                                    "duration": 7_260_000,  # 121 minutes, in ms
                                    # Deliberately a different path shape than relocarr's own
                                    # scan would ever report (Plex sees its own local storage
                                    # path, not the CIFS mount path) -- the point of this test.
                                    "Part": [{"file": "/storage/xyz/movies/Example Feature.1982.mkv"}],
                                }
                            ],
                        }
                    ]
                }
            },
        )
    )

    index = client.build_quality_index()
    client.close()

    assert movies_route.called
    assert index == {
        # Keyed by basename only -- matches relocarr's own scan's path for
        # the same file despite the two being completely different above.
        "Example Feature.1982.mkv": {
            "resolution": "1080",
            "codec": "hevc",
            "bitrate": 8000,
            "duration_ms": 7_260_000,
            "rating_key": 501,
        }
    }
    # the TV section (key "2") was never requested
    assert "http://plex:32400/library/sections/2/all" not in [
        str(c.request.url) for c in respx.calls
    ]


@respx.mock
def test_get_machine_identifier():
    client = PlexClient("http://plex:32400", "tok")
    respx.get("http://plex:32400/identity").mock(
        return_value=httpx.Response(
            200, json={"MediaContainer": {"machineIdentifier": "abc123"}}
        )
    )

    result = client.get_machine_identifier()
    client.close()

    assert result == "abc123"


@respx.mock
def test_fetch_poster_uses_the_transcode_endpoint_with_an_unversioned_thumb_path():
    # Confirmed live, 2026-10-28: the full-size original
    # (/library/metadata/{id}/thumb) runs multi-megabyte, far too large for
    # a 40-140 row table -- the transcode endpoint returns a resized JPEG
    # instead, and the un-versioned thumb path (no trailing timestamp) was
    # confirmed to still resolve to the current poster.
    client = PlexClient("http://plex:32400", "tok")
    route = respx.get("http://plex:32400/photo/:/transcode").mock(
        return_value=httpx.Response(200, content=b"fake-jpeg-bytes", headers={"content-type": "image/jpeg"})
    )

    content, content_type = client.fetch_poster(501, width=72, height=108)
    client.close()

    assert content == b"fake-jpeg-bytes"
    assert content_type == "image/jpeg"
    sent_params = dict(route.calls[0].request.url.params)
    assert sent_params["url"] == "/library/metadata/501/thumb"
    assert sent_params["width"] == "72"
    assert sent_params["height"] == "108"


def test_build_plex_web_url():
    url = build_plex_web_url("http://plex:32400/", "abc123", 501)
    assert url == "http://plex:32400/web/index.html#!/server/abc123/details?key=/library/metadata/501"


@respx.mock
def test_list_duplicate_groups_only_returns_items_with_more_than_one_file():
    client = PlexClient("http://plex:32400", "tok")
    respx.get("http://plex:32400/library/sections").mock(
        return_value=httpx.Response(
            200,
            json={
                "MediaContainer": {
                    "Directory": [
                        {"key": "1", "type": "movie", "title": "Movies"},
                        {"key": "2", "type": "show", "title": "TV Shows"},
                    ]
                }
            },
        )
    )
    dup_route = respx.get(
        "http://plex:32400/library/sections/1/all", params={"duplicate": 1}
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "MediaContainer": {
                    "Metadata": [
                        {
                            "title": "Example Feature",
                            "Media": [
                                {"Part": [{"file": "/storage/xyz/movies/Example Feature (1982).mkv", "size": 100}]},
                                {"Part": [{"file": "/storage/xyz/movies/Example Feature.1982.mkv", "size": 200}]},
                            ],
                        },
                        {
                            # Only one file -- Plex itself doesn't consider this a duplicate,
                            # confirming list_duplicate_groups trusts Plex's own filter rather
                            # than re-checking the count itself.
                            "title": "Solo Movie",
                            "Media": [
                                {"Part": [{"file": "/storage/xyz/movies/Solo Movie.1999.mkv", "size": 100}]}
                            ],
                        },
                    ]
                }
            },
        )
    )

    groups = client.list_duplicate_groups()
    client.close()

    assert dup_route.called
    assert groups == [
        [
            {"name": "Example Feature (1982).mkv", "size_bytes": 100},
            {"name": "Example Feature.1982.mkv", "size_bytes": 200},
        ]
    ]
    assert "http://plex:32400/library/sections/2/all" not in [
        str(c.request.url) for c in respx.calls
    ]


@respx.mock
def test_refresh_movie_sections_only_refreshes_movie_sections():
    client = PlexClient("http://plex:32400", "tok")
    respx.get("http://plex:32400/library/sections").mock(
        return_value=httpx.Response(
            200,
            json={
                "MediaContainer": {
                    "Directory": [
                        {"key": "1", "type": "movie", "title": "Movies"},
                        {"key": "2", "type": "show", "title": "TV Shows"},
                    ]
                }
            },
        )
    )
    refresh_route = respx.post("http://plex:32400/library/sections/1/refresh").mock(
        return_value=httpx.Response(200)
    )

    client.refresh_movie_sections()
    client.close()

    assert refresh_route.called
    assert "http://plex:32400/library/sections/2/refresh" not in [
        str(c.request.url) for c in respx.calls
    ]


@pytest.mark.asyncio
async def test_run_plex_quality_check_forever_does_nothing_when_unconfigured(tmp_path):
    db_path = tmp_path / "relocarr.db"
    init_db(db_path)

    cache = PlexQualityCache()
    stop_event = asyncio.Event()
    task = asyncio.create_task(run_plex_quality_check_forever(db_path, cache, stop_event))
    await asyncio.sleep(0.2)
    stop_event.set()
    await asyncio.wait_for(task, timeout=2)

    assert cache.index == {}
    assert cache.duplicate_groups == []
    assert cache.checked_at is None


@pytest.mark.asyncio
@respx.mock
async def test_run_plex_quality_check_forever_populates_the_cache(tmp_path):
    db_path = tmp_path / "relocarr.db"
    init_db(db_path)
    with session(db_path) as conn:
        conn.execute(
            "UPDATE settings SET value = 'http://plex:32400' WHERE key = 'plex_base_url'"
        )
        conn.execute("UPDATE settings SET value = 'tok' WHERE key = 'plex_token'")

    respx.get("http://plex:32400/library/sections").mock(
        return_value=httpx.Response(200, json={"MediaContainer": {"Directory": []}})
    )
    respx.get("http://plex:32400/identity").mock(
        return_value=httpx.Response(200, json={"MediaContainer": {"machineIdentifier": "abc123"}})
    )

    cache = PlexQualityCache()
    stop_event = asyncio.Event()
    task = asyncio.create_task(run_plex_quality_check_forever(db_path, cache, stop_event))
    await asyncio.sleep(0.2)
    stop_event.set()
    await asyncio.wait_for(task, timeout=2)

    assert cache.index == {}
    assert cache.duplicate_groups == []
    assert cache.checked_at is not None
    assert cache.error is None
    assert cache.base_url == "http://plex:32400"
    assert cache.machine_identifier == "abc123"
