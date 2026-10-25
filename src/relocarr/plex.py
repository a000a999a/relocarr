from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import httpx

from .arr.base import ConnectionTestResult

logger = logging.getLogger(__name__)

# Plex's own library-scan cadence has no API to hook into directly, so this
# just matches library.py's own movie-folder scan interval -- no point
# refreshing quality info more often than the file list it's being matched
# against changes.
CHECK_INTERVAL_SECONDS = 600.0
# A full library listing (GET /library/sections/{id}/all) returns every
# field for every movie in one response -- can be slow for a large library,
# unlike the single-item lookups the rest of this app makes against Radarr.
CHECK_TIMEOUT_SECONDS = 30.0


class PlexClient:
    """Plex Media Server API client. Endpoints and field names follow Plex's
    long-stable, widely-documented JSON API shape (the same one python-plexapi
    parses) -- not yet confirmed live against this project's own Plex
    instance, since that requires a real server URL + X-Plex-Token the user
    provides through Settings -> Plex; Test Connection there is the first
    real-world check.
    """

    def __init__(self, base_url: str, token: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={"X-Plex-Token": token, "Accept": "application/json"},
            timeout=timeout,
        )

    def test_connection(self) -> ConnectionTestResult:
        """Deliberately NOT /identity -- confirmed live, 2026-10-25: Plex
        answers that one without checking the token at all, so a bad or
        malformed token (a common copy-paste mistake: pasting the whole
        "X-Plex-Token=..." query-string fragment instead of just the
        value after the =) still reported a successful "Connected" status
        here while the real quality lookup's own /library/sections call
        failed with 401. /library/sections genuinely requires a valid
        token, so a bad one is now caught right here, at save time.
        """
        try:
            resp = self._client.get("/library/sections")
            resp.raise_for_status()
            version = resp.json().get("MediaContainer", {}).get("version")
            return ConnectionTestResult(ok=True, version=version)
        except httpx.HTTPError as exc:
            return ConnectionTestResult(ok=False, error=str(exc))

    def build_quality_index(self) -> dict[str, dict]:
        """Every movie library section's items, indexed by each file's real
        filesystem path (Media[].Part[].file -- the same absolute path
        relocarr's own library scan already uses, since both read the
        identical bind-mounted target folder). One GET /library/sections
        plus one GET /library/sections/{id}/all per movie-type section,
        not one call per file.
        """
        sections_resp = self._client.get("/library/sections")
        sections_resp.raise_for_status()
        movie_section_keys = [
            d["key"]
            for d in sections_resp.json().get("MediaContainer", {}).get("Directory", [])
            if d.get("type") == "movie"
        ]

        index: dict[str, dict] = {}
        for key in movie_section_keys:
            resp = self._client.get(f"/library/sections/{key}/all")
            resp.raise_for_status()
            for item in resp.json().get("MediaContainer", {}).get("Metadata", []):
                for media in item.get("Media", []):
                    quality = {
                        "resolution": media.get("videoResolution"),
                        "codec": media.get("videoCodec"),
                        "bitrate": media.get("bitrate"),
                    }
                    for part in media.get("Part", []):
                        path = part.get("file")
                        if path:
                            index[path] = quality
        return index

    def close(self) -> None:
        self._client.close()


class PlexQualityCache:
    """Mirrors disk_health.py/library.py/version_check.py: a live Plex API
    call (here, potentially a large library listing) has no business
    running inside a web request. Read-only for every web request; only
    the background loop below ever calls Plex.
    """

    def __init__(self) -> None:
        self.index: dict[str, dict] = {}
        self.checked_at: float | None = None
        self.error: str | None = None


cache = PlexQualityCache()


def _check(db_path: Path) -> dict[str, dict] | None:
    """Returns None (not an error) when Plex isn't configured yet at all --
    distinct from a configured-but-failing connection, which sets
    cache.error instead of silently leaving the index empty.
    """
    from .db import session

    with session(db_path) as conn:
        rows = {
            r["key"]: r["value"]
            for r in conn.execute(
                "SELECT key, value FROM settings WHERE key IN ('plex_base_url', 'plex_token')"
            ).fetchall()
        }
    base_url = rows.get("plex_base_url", "")
    token = rows.get("plex_token", "")
    if not base_url or not token:
        return None

    client = PlexClient(base_url, token)
    try:
        return client.build_quality_index()
    finally:
        client.close()


async def run_plex_quality_check_forever(
    db_path: Path, cache: PlexQualityCache, stop_event: asyncio.Event
) -> None:
    while not stop_event.is_set():
        try:
            index = await asyncio.wait_for(asyncio.to_thread(_check, db_path), timeout=CHECK_TIMEOUT_SECONDS)
            if index is not None:
                cache.index = index
                cache.checked_at = time.time()
                cache.error = None
        except TimeoutError:
            logger.warning("Plex quality check timed out after %ss", CHECK_TIMEOUT_SECONDS)
            cache.error = "timed out"
        except Exception as exc:  # noqa: BLE001 -- a failed check must not kill the loop
            logger.warning("Plex quality check failed: %s", exc)
            cache.error = str(exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=CHECK_INTERVAL_SECONDS)
        except TimeoutError:
            pass
