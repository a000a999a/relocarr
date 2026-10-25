import asyncio

import httpx
import pytest
import respx

from relocarr.version_check import (
    TAGS_URL,
    VersionCheckCache,
    fetch_latest_tag,
    is_newer,
    parse_version,
    run_version_check_forever,
)


def test_parse_version_handles_v_prefix():
    assert parse_version("v1.0.0") == (1, 0, 0)


def test_parse_version_handles_no_prefix():
    assert parse_version("1.2.3") == (1, 2, 3)


def test_parse_version_returns_none_for_non_semver_tag():
    assert parse_version("latest") is None
    assert parse_version("v1.0") is None


def test_is_newer_true_when_latest_has_a_higher_version():
    assert is_newer("v1.1.0", "1.0.0") is True


def test_is_newer_false_when_versions_are_equal():
    assert is_newer("v1.0.0", "1.0.0") is False


def test_is_newer_false_when_latest_is_older():
    assert is_newer("v0.9.0", "1.0.0") is False


def test_is_newer_false_for_an_unparseable_tag():
    assert is_newer("latest", "1.0.0") is False


@respx.mock
def test_fetch_latest_tag_picks_the_highest_semver_tag():
    respx.get(TAGS_URL).mock(
        return_value=httpx.Response(
            200, json=[{"name": "v0.1.0"}, {"name": "v1.0.0"}, {"name": "v0.9.0"}]
        )
    )
    assert fetch_latest_tag() == "v1.0.0"


@respx.mock
def test_fetch_latest_tag_ignores_non_semver_tags():
    respx.get(TAGS_URL).mock(
        return_value=httpx.Response(200, json=[{"name": "latest"}, {"name": "v1.0.0"}])
    )
    assert fetch_latest_tag() == "v1.0.0"


@respx.mock
def test_fetch_latest_tag_returns_none_when_nothing_parses():
    respx.get(TAGS_URL).mock(return_value=httpx.Response(200, json=[{"name": "latest"}]))
    assert fetch_latest_tag() is None


@pytest.mark.asyncio
@respx.mock
async def test_run_version_check_forever_populates_the_cache():
    respx.get(TAGS_URL).mock(return_value=httpx.Response(200, json=[{"name": "v1.0.0"}]))

    cache = VersionCheckCache()
    stop_event = asyncio.Event()
    task = asyncio.create_task(run_version_check_forever(cache, stop_event))
    await asyncio.sleep(0.2)
    stop_event.set()
    await asyncio.wait_for(task, timeout=2)

    assert cache.latest_version == "v1.0.0"
    assert cache.checked_at is not None
    assert cache.error is None


@pytest.mark.asyncio
async def test_run_version_check_forever_records_error_on_failure(monkeypatch):
    def _boom():
        raise httpx.HTTPError("boom")

    monkeypatch.setattr("relocarr.version_check.fetch_latest_tag", _boom)
    monkeypatch.setattr("relocarr.version_check.CHECK_INTERVAL_SECONDS", 10.0)

    cache = VersionCheckCache()
    stop_event = asyncio.Event()
    task = asyncio.create_task(run_version_check_forever(cache, stop_event))
    await asyncio.sleep(0.2)
    stop_event.set()
    await asyncio.wait_for(task, timeout=2)

    assert cache.latest_version is None
    assert cache.error is not None
