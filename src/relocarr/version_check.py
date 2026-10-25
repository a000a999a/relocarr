from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx

from . import VERSION

logger = logging.getLogger(__name__)

TAGS_URL = "https://api.github.com/repos/a000a999a/relocarr/tags"

# Releases happen on the order of days, not minutes -- no need to hammer
# GitHub's API. Generous enough that a transient GitHub outage doesn't
# matter, short enough that a real new release shows up the same day.
CHECK_INTERVAL_SECONDS = 21600.0  # 6 hours
CHECK_TIMEOUT_SECONDS = 10.0

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def parse_version(v: str) -> tuple[int, int, int] | None:
    m = _VERSION_RE.match(v.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def is_newer(latest: str, current: str) -> bool:
    latest_parsed = parse_version(latest)
    current_parsed = parse_version(current)
    if latest_parsed is None or current_parsed is None:
        return False
    return latest_parsed > current_parsed


def fetch_latest_tag() -> str | None:
    """GitHub's tags API, not /releases/latest -- this project tags
    releases with plain `git tag` + `git push --tags` and doesn't always
    also create a formal GitHub Release object for each one (/releases/latest
    404s unless one exists), so tags are the only thing guaranteed to be
    there. Every tag is parsed as `vMAJOR.MINOR.PATCH`; anything that
    doesn't match that shape (a stray non-release tag) is ignored rather
    than guessed at. Returns the highest by semver comparison, or None if
    nothing parses or the request fails.
    """
    resp = httpx.get(
        TAGS_URL, timeout=CHECK_TIMEOUT_SECONDS, headers={"Accept": "application/vnd.github+json"}
    )
    resp.raise_for_status()
    tags = [t["name"] for t in resp.json()]
    parsed = [(parse_version(t), t) for t in tags]
    valid = [(v, t) for v, t in parsed if v is not None]
    if not valid:
        return None
    return max(valid, key=lambda pair: pair[0])[1]


class VersionCheckCache:
    """Mirrors disk_health.py's DiskSpaceCache and library.py's
    MovieLibraryCache: a live network call (here, to GitHub's API) has no
    business running inside a web request -- see those modules for the
    "a wedged/slow external call must never hang a page" rationale this
    whole pattern exists for. Read-only for every web request; only the
    background loop below ever calls GitHub.
    """

    def __init__(self) -> None:
        self.latest_version: str | None = None
        self.checked_at: float | None = None
        self.error: str | None = None


cache = VersionCheckCache()


async def run_version_check_forever(cache: VersionCheckCache, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            latest = await asyncio.wait_for(
                asyncio.to_thread(fetch_latest_tag), timeout=CHECK_TIMEOUT_SECONDS
            )
            cache.latest_version = latest
            cache.checked_at = time.time()
            cache.error = None
        except TimeoutError:
            logger.warning("version check timed out after %ss", CHECK_TIMEOUT_SECONDS)
            cache.error = "timed out"
        except Exception as exc:  # noqa: BLE001 -- a failed check must not kill the loop
            logger.warning("version check failed: %s", exc)
            cache.error = str(exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=CHECK_INTERVAL_SECONDS)
        except TimeoutError:
            pass
