import json
from unittest.mock import patch

import httpx
import respx

from relocarr.arr.base import MovieMatch, QualityProfile, Release
from relocarr.arr.radarr import RELEASE_SEARCH_TIMEOUT_SECONDS, RadarrApp


@respx.mock
def test_list_quality_profiles():
    client = RadarrApp("http://radarr:7878", "key")
    respx.get("http://radarr:7878/api/v3/qualityprofile").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "name": "HD-1080p"}, {"id": 2, "name": "Small"}])
    )

    profiles = client.list_quality_profiles()
    client.close()

    assert profiles == [QualityProfile(id=1, name="HD-1080p"), QualityProfile(id=2, name="Small")]


@respx.mock
def test_build_movie_index_by_path_only_includes_movies_with_a_file():
    client = RadarrApp("http://radarr:7878", "key")
    respx.get("http://radarr:7878/api/v3/movie").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 42,
                    "title": "Example Feature",
                    "year": 1982,
                    "qualityProfileId": 1,
                    "monitored": True,
                    "movieFile": {"id": 99, "path": "/mnt/nvidia/movies/Example Feature.mkv"},
                },
                {
                    "id": 7,
                    "title": "Big File Movie",
                    "year": 2026,
                    "qualityProfileId": 1,
                    "monitored": False,
                },  # relocarr already processed this one -- no movieFile left
            ],
        )
    )

    by_path, by_title_year = client.build_movie_index()
    client.close()

    assert by_path == {
        "/mnt/nvidia/movies/Example Feature.mkv": MovieMatch(
            movie_id=42,
            movie_file_id=99,
            quality_profile_id=1,
            title="Example Feature",
            monitored=True,
            has_file=True,
        )
    }
    assert by_title_year[("big file movie", 2026)] == MovieMatch(
        movie_id=7, movie_file_id=None, quality_profile_id=1, title="Big File Movie", monitored=False, has_file=False
    )
    assert ("example feature", 1982) in by_title_year


@respx.mock
def test_find_movie_by_file_path_returns_none_when_unmatched():
    client = RadarrApp("http://radarr:7878", "key")
    respx.get("http://radarr:7878/api/v3/movie").mock(return_value=httpx.Response(200, json=[]))

    result = client.find_movie_by_file_path("/does/not/exist.mkv")
    client.close()

    assert result is None


@respx.mock
def test_set_quality_profile_updates_only_that_field():
    client = RadarrApp("http://radarr:7878", "key")
    respx.get("http://radarr:7878/api/v3/movie/42").mock(
        return_value=httpx.Response(200, json={"id": 42, "qualityProfileId": 1, "monitored": True, "title": "X"})
    )
    put_route = respx.put("http://radarr:7878/api/v3/movie/42").mock(return_value=httpx.Response(200))

    client.set_quality_profile(42, 6)
    client.close()

    sent = json.loads(put_route.calls.last.request.content)
    assert sent["qualityProfileId"] == 6
    assert sent["monitored"] is True  # untouched


@respx.mock
def test_set_monitored_sets_monitored_true():
    client = RadarrApp("http://radarr:7878", "key")
    respx.get("http://radarr:7878/api/v3/movie/7").mock(
        return_value=httpx.Response(200, json={"id": 7, "monitored": False, "qualityProfileId": 1, "title": "X"})
    )
    put_route = respx.put("http://radarr:7878/api/v3/movie/7").mock(return_value=httpx.Response(200))

    client.set_monitored(7)
    client.close()

    sent = json.loads(put_route.calls.last.request.content)
    assert sent["monitored"] is True
    assert sent["qualityProfileId"] == 1  # untouched


def _release(guid, size, *, seeders=50, resolution=1080, rejections=None, title=None):
    return {
        "guid": guid,
        "indexerId": 1,
        "size": size,
        "title": title or guid,
        "seeders": seeders,
        "quality": {"quality": {"resolution": resolution}},
        "rejections": rejections or [],
    }


@respx.mock
def test_find_smaller_release_uses_a_longer_timeout_for_the_live_indexer_search():
    # Confirmed live, 2026-10-24: a real release search can take longer
    # than the client's normal 10s default (Radarr queries every indexer
    # live before responding), and the resulting timeout was misreported
    # to the user as "Could not reach Radarr" when Radarr was never
    # actually down.
    client = RadarrApp("http://radarr:7878", "key")
    respx.get("http://radarr:7878/api/v3/release").mock(return_value=httpx.Response(200, json=[]))

    with patch.object(client._client, "get", wraps=client._client.get) as mock_get:
        client.find_smaller_release(movie_id=42, current_size_bytes=1000)
    client.close()

    _, kwargs = mock_get.call_args
    assert kwargs["timeout"] == RELEASE_SEARCH_TIMEOUT_SECONDS


@respx.mock
def test_find_smaller_release_picks_the_smallest_non_rejected_candidate():
    client = RadarrApp("http://radarr:7878", "key")
    respx.get("http://radarr:7878/api/v3/release").mock(
        return_value=httpx.Response(
            200,
            json=[
                _release("big", 4 * 1024**3, title="Big Release"),  # not smaller than current -- excluded
                _release(
                    "small-but-wrong-language",
                    1 * 1024**3,
                    title="Small Wrong Language",
                    rejections=["Unwanted language"],  # a real disqualifier -- excluded
                ),
                _release(
                    "smaller",
                    int(1.5 * 1024**3),
                    title="Smaller Release",
                    rejections=["Existing file meets cutoff: HDTV-720p []"],  # override-able
                ),
                _release(
                    "smallest",
                    1 * 1024**3,
                    title="Smallest Release",
                    rejections=["Existing file and the Quality profile does not allow upgrades"],  # override-able
                ),
            ],
        )
    )

    result = client.find_smaller_release(movie_id=42, current_size_bytes=4 * 1024**3)
    client.close()

    assert result == Release(guid="smallest", indexer_id=1, size_bytes=1 * 1024**3, title="Smallest Release")


@respx.mock
def test_find_smaller_release_returns_none_when_nothing_is_meaningfully_smaller():
    client = RadarrApp("http://radarr:7878", "key")
    respx.get("http://radarr:7878/api/v3/release").mock(
        return_value=httpx.Response(
            200,
            # only 1% smaller -- not worth it
            json=[_release("almost-same-size", int(4 * 1024**3 * 0.99))],
        )
    )

    result = client.find_smaller_release(movie_id=42, current_size_bytes=4 * 1024**3)
    client.close()

    assert result is None


@respx.mock
def test_find_smaller_release_returns_none_when_everything_is_disqualified():
    client = RadarrApp("http://radarr:7878", "key")
    respx.get("http://radarr:7878/api/v3/release").mock(
        return_value=httpx.Response(
            200,
            json=[_release("scene-release-banned", 1 * 1024**3, rejections=["Scene release not allowed"])],
        )
    )

    result = client.find_smaller_release(movie_id=42, current_size_bytes=4 * 1024**3)
    client.close()

    assert result is None


@respx.mock
def test_find_smaller_release_excludes_zero_seeder_releases():
    # A dead torrent can never actually finish downloading.
    client = RadarrApp("http://radarr:7878", "key")
    respx.get("http://radarr:7878/api/v3/release").mock(
        return_value=httpx.Response(
            200,
            json=[
                _release("dead", 1 * 1024**3, seeders=0),
                _release("alive", int(1.5 * 1024**3), seeders=5),
            ],
        )
    )

    result = client.find_smaller_release(movie_id=42, current_size_bytes=4 * 1024**3)
    client.close()

    assert result.guid == "alive"


@respx.mock
def test_find_smaller_release_prefers_healthy_seeders_over_smaller_size():
    # A well-seeded larger release beats a barely-seeded smaller one --
    # "if there's only a seeder for larger files, I accept a larger file".
    client = RadarrApp("http://radarr:7878", "key")
    respx.get("http://radarr:7878/api/v3/release").mock(
        return_value=httpx.Response(
            200,
            json=[
                _release("tiny-but-dead", int(1 * 1024**3), seeders=1),
                _release("bigger-but-healthy", int(2.5 * 1024**3), seeders=50),
            ],
        )
    )

    result = client.find_smaller_release(movie_id=42, current_size_bytes=4 * 1024**3)
    client.close()

    assert result.guid == "bigger-but-healthy"


@respx.mock
def test_find_smaller_release_falls_back_to_best_seeded_when_none_are_healthy():
    # Nothing clears the healthy-seeder bar -- still picks the best of
    # what's available rather than returning None.
    client = RadarrApp("http://radarr:7878", "key")
    respx.get("http://radarr:7878/api/v3/release").mock(
        return_value=httpx.Response(
            200,
            json=[
                _release("two-seeders", int(1.5 * 1024**3), seeders=2),
                _release("one-seeder", int(1 * 1024**3), seeders=1),
            ],
        )
    )

    result = client.find_smaller_release(movie_id=42, current_size_bytes=4 * 1024**3)
    client.close()

    assert result.guid == "two-seeders"


@respx.mock
def test_find_smaller_release_prefers_higher_resolution_among_equally_healthy_options():
    client = RadarrApp("http://radarr:7878", "key")
    respx.get("http://radarr:7878/api/v3/release").mock(
        return_value=httpx.Response(
            200,
            json=[
                _release("720p-smaller", int(1 * 1024**3), seeders=50, resolution=720),
                _release("1080p-bigger", int(1.5 * 1024**3), seeders=50, resolution=1080),
            ],
        )
    )

    result = client.find_smaller_release(movie_id=42, current_size_bytes=4 * 1024**3)
    client.close()

    assert result.guid == "1080p-bigger"


@respx.mock
def test_find_smaller_release_picks_smallest_within_the_same_resolution_tier():
    client = RadarrApp("http://radarr:7878", "key")
    respx.get("http://radarr:7878/api/v3/release").mock(
        return_value=httpx.Response(
            200,
            json=[
                _release("1080p-bigger", int(2 * 1024**3), seeders=50, resolution=1080),
                _release("1080p-smaller", int(1 * 1024**3), seeders=50, resolution=1080),
            ],
        )
    )

    result = client.find_smaller_release(movie_id=42, current_size_bytes=4 * 1024**3)
    client.close()

    assert result.guid == "1080p-smaller"


@respx.mock
def test_grab_release_posts_guid_and_indexer_id():
    client = RadarrApp("http://radarr:7878", "key")
    grab_route = respx.post("http://radarr:7878/api/v3/release").mock(return_value=httpx.Response(200))

    client.grab_release(Release(guid="abc", indexer_id=3, size_bytes=100, title="X"))
    client.close()

    sent = json.loads(grab_route.calls.last.request.content)
    assert sent == {"guid": "abc", "indexerId": 3}


@respx.mock
def test_lookup_and_add_finds_the_matching_year_and_adds_it():
    client = RadarrApp("http://radarr:7878", "key")
    respx.get("http://radarr:7878/api/v3/movie/lookup").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"title": "Example Feature", "year": 1999, "tmdbId": 1},  # wrong year -- skip
                {"title": "Example Feature", "year": 1982, "tmdbId": 54321},
            ],
        )
    )
    add_route = respx.post("http://radarr:7878/api/v3/movie").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": 50,
                "title": "Example Feature",
                "qualityProfileId": 6,
                "year": 1982,
            },
        )
    )

    result = client.lookup_and_add(
        "Example Feature", 1982, quality_profile_id=6, root_folder_path="/staging/movies"
    )
    client.close()

    assert result == MovieMatch(
        movie_id=50,
        movie_file_id=None,
        quality_profile_id=6,
        title="Example Feature",
        monitored=True,
        has_file=False,
    )
    sent = json.loads(add_route.calls.last.request.content)
    assert sent["tmdbId"] == 54321  # the 1982 one, not the 1999 decoy
    assert sent["qualityProfileId"] == 6
    assert sent["rootFolderPath"] == "/staging/movies"
    assert sent["monitored"] is True
    assert sent["addOptions"] == {"searchForMovie": True}  # default -- unchanged behavior for other callers


@respx.mock
def test_lookup_and_add_can_skip_radarrs_own_auto_search():
    client = RadarrApp("http://radarr:7878", "key")
    respx.get("http://radarr:7878/api/v3/movie/lookup").mock(
        return_value=httpx.Response(200, json=[{"title": "Example Feature", "year": 1982, "tmdbId": 1}])
    )
    add_route = respx.post("http://radarr:7878/api/v3/movie").mock(
        return_value=httpx.Response(201, json={"id": 50, "title": "Example Feature", "qualityProfileId": 6})
    )

    client.lookup_and_add(
        "Example Feature", 1982, quality_profile_id=6, root_folder_path="/s", search_for_movie=False
    )
    client.close()

    sent = json.loads(add_route.calls.last.request.content)
    assert sent["addOptions"] == {"searchForMovie": False}


@respx.mock
def test_lookup_and_add_returns_none_when_lookup_finds_nothing():
    client = RadarrApp("http://radarr:7878", "key")
    respx.get("http://radarr:7878/api/v3/movie/lookup").mock(return_value=httpx.Response(200, json=[]))

    result = client.lookup_and_add("Nonexistent Movie", 2099, quality_profile_id=6, root_folder_path="/s")
    client.close()

    assert result is None
