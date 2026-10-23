import json

import httpx
import respx

from relocarr.arr.base import MovieMatch, QualityProfile
from relocarr.arr.radarr import RadarrApp


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
def test_redownload_movie_deletes_file_and_triggers_search():
    client = RadarrApp("http://radarr:7878", "key")
    delete_route = respx.delete("http://radarr:7878/api/v3/moviefile/99").mock(
        return_value=httpx.Response(200)
    )
    command_route = respx.post("http://radarr:7878/api/v3/command").mock(
        return_value=httpx.Response(201, json={"id": 1})
    )

    client.redownload_movie(movie_id=42, movie_file_id=99)
    client.close()

    assert delete_route.called
    assert command_route.called
    sent = json.loads(command_route.calls.last.request.content)
    assert sent == {"name": "MoviesSearch", "movieIds": [42]}


@respx.mock
def test_redownload_movie_updates_quality_profile_first_when_given():
    client = RadarrApp("http://radarr:7878", "key")
    respx.get("http://radarr:7878/api/v3/movie/42").mock(
        return_value=httpx.Response(200, json={"id": 42, "qualityProfileId": 1, "title": "X"})
    )
    put_route = respx.put("http://radarr:7878/api/v3/movie/42").mock(return_value=httpx.Response(200))
    respx.delete("http://radarr:7878/api/v3/moviefile/99").mock(return_value=httpx.Response(200))
    respx.post("http://radarr:7878/api/v3/command").mock(return_value=httpx.Response(201))

    client.redownload_movie(movie_id=42, movie_file_id=99, quality_profile_id=5)
    client.close()

    sent = json.loads(put_route.calls.last.request.content)
    assert sent["qualityProfileId"] == 5


@respx.mock
def test_redownload_movie_skips_file_delete_when_no_file():
    client = RadarrApp("http://radarr:7878", "key")
    delete_route = respx.delete(url__regex=r".*/api/v3/moviefile/.*").mock(return_value=httpx.Response(200))
    command_route = respx.post("http://radarr:7878/api/v3/command").mock(return_value=httpx.Response(201))

    client.redownload_movie(movie_id=42, movie_file_id=None)
    client.close()

    assert not delete_route.called
    assert command_route.called


@respx.mock
def test_monitor_and_search_sets_monitored_true_and_searches():
    client = RadarrApp("http://radarr:7878", "key")
    respx.get("http://radarr:7878/api/v3/movie/7").mock(
        return_value=httpx.Response(200, json={"id": 7, "monitored": False, "qualityProfileId": 1, "title": "X"})
    )
    put_route = respx.put("http://radarr:7878/api/v3/movie/7").mock(return_value=httpx.Response(200))
    command_route = respx.post("http://radarr:7878/api/v3/command").mock(return_value=httpx.Response(201))

    client.monitor_and_search(movie_id=7)
    client.close()

    sent = json.loads(put_route.calls.last.request.content)
    assert sent["monitored"] is True
    assert "qualityProfileId" not in sent or sent["qualityProfileId"] == 1  # unchanged when not given
    assert command_route.called


@respx.mock
def test_monitor_and_search_also_updates_quality_profile_when_given():
    client = RadarrApp("http://radarr:7878", "key")
    respx.get("http://radarr:7878/api/v3/movie/7").mock(
        return_value=httpx.Response(200, json={"id": 7, "monitored": False, "qualityProfileId": 1, "title": "X"})
    )
    put_route = respx.put("http://radarr:7878/api/v3/movie/7").mock(return_value=httpx.Response(200))
    respx.post("http://radarr:7878/api/v3/command").mock(return_value=httpx.Response(201))

    client.monitor_and_search(movie_id=7, quality_profile_id=6)
    client.close()

    sent = json.loads(put_route.calls.last.request.content)
    assert sent["monitored"] is True
    assert sent["qualityProfileId"] == 6


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
    assert sent["addOptions"] == {"searchForMovie": True}


@respx.mock
def test_lookup_and_add_returns_none_when_lookup_finds_nothing():
    client = RadarrApp("http://radarr:7878", "key")
    respx.get("http://radarr:7878/api/v3/movie/lookup").mock(return_value=httpx.Response(200, json=[]))

    result = client.lookup_and_add("Nonexistent Movie", 2099, quality_profile_id=6, root_folder_path="/s")
    client.close()

    assert result is None
