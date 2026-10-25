import asyncio

import httpx
import pytest
import respx

from relocarr.config import settings as app_settings
from relocarr.db import init_db, session
from relocarr.plex import PlexClient, PlexQualityCache, run_plex_quality_check_forever


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
                            "Media": [
                                {
                                    "videoResolution": "1080",
                                    "videoCodec": "hevc",
                                    "bitrate": 8000,
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
        }
    }
    # the TV section (key "2") was never requested
    assert "http://plex:32400/library/sections/2/all" not in [
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

    cache = PlexQualityCache()
    stop_event = asyncio.Event()
    task = asyncio.create_task(run_plex_quality_check_forever(db_path, cache, stop_event))
    await asyncio.sleep(0.2)
    stop_event.set()
    await asyncio.wait_for(task, timeout=2)

    assert cache.index == {}
    assert cache.checked_at is not None
    assert cache.error is None
