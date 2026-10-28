from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path, PurePosixPath

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
        """Every movie library section's items, indexed by each file's
        BASENAME, not its full path -- confirmed live, 2026-10-25: Plex
        (running on the Shield itself, with its own local storage path,
        e.g. /storage/3ACB-9090/movies/...) reports a completely different
        absolute path than relocarr's own library scan sees for the exact
        same file (via the CIFS mount, e.g. /mnt/nvidia/movies/...) -- a
        full-path index never matched anything. Filenames always carry
        title and year (Radarr's own naming format guarantees this), so a
        basename collision between two actually-different movies isn't a
        realistic risk here. One GET /library/sections plus one GET
        /library/sections/{id}/all per movie-type section, not one call
        per file.
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
                            index[PurePosixPath(path).name] = quality
        return index

    def list_duplicate_groups(self) -> list[list[dict]]:
        """Each returned group is one movie Plex's own metadata agent has
        matched more than one file to, straight from Plex's `duplicate=1`
        library filter -- the exact same source its web UI's "Duplicates"
        view uses -- rather than relocarr re-deriving "which files are the
        same movie" itself from filenames. Confirmed live, 2026-10-28:
        this catches cases a (title, year) filename regex gets wrong or
        misses outright -- e.g. two different naming conventions for the
        same release, or a file whose name carries the wrong year --
        because it's matched by Plex's real metadata lookup, not a
        pattern over the filename. One GET /library/sections plus one GET
        /library/sections/{id}/all?duplicate=1 per movie-type section.
        """
        sections_resp = self._client.get("/library/sections")
        sections_resp.raise_for_status()
        movie_section_keys = [
            d["key"]
            for d in sections_resp.json().get("MediaContainer", {}).get("Directory", [])
            if d.get("type") == "movie"
        ]

        groups: list[list[dict]] = []
        for key in movie_section_keys:
            resp = self._client.get(f"/library/sections/{key}/all", params={"duplicate": 1})
            resp.raise_for_status()
            for item in resp.json().get("MediaContainer", {}).get("Metadata", []):
                files = []
                for media in item.get("Media", []):
                    for part in media.get("Part", []):
                        path = part.get("file")
                        if path:
                            files.append(
                                {"name": PurePosixPath(path).name, "size_bytes": part.get("size") or 0}
                            )
                if len(files) > 1:
                    groups.append(files)
        return groups

    def refresh_movie_sections(self) -> None:
        """Tells Plex to start a library scan -- the same action as its own
        "Scan Library Files" button -- without waiting for that scan to
        finish. Safe to call inline from a request handler, unlike
        build_quality_index/list_duplicate_groups: those fetch a full
        library listing back, this is a single quick POST Plex
        acknowledges immediately and scans in the background on its own
        side. Confirmed live, 2026-10-28.
        """
        sections_resp = self._client.get("/library/sections")
        sections_resp.raise_for_status()
        movie_section_keys = [
            d["key"]
            for d in sections_resp.json().get("MediaContainer", {}).get("Directory", [])
            if d.get("type") == "movie"
        ]
        for key in movie_section_keys:
            resp = self._client.post(f"/library/sections/{key}/refresh")
            resp.raise_for_status()

    def close(self) -> None:
        self._client.close()


class PlexQualityCache:
    """Mirrors disk_health.py/library.py/version_check.py: a live Plex API
    call (here, potentially a large library listing) has no business
    running inside a web request. Read-only for every web request; only
    the background loop below ever calls Plex. Holds Plex's duplicate
    groups alongside the quality index -- both come from the same
    per-cycle Plex check, there's no reason to run two separate loops
    against the same server.
    """

    def __init__(self) -> None:
        self.index: dict[str, dict] = {}
        self.duplicate_groups: list[list[dict]] = []
        self.checked_at: float | None = None
        self.error: str | None = None


cache = PlexQualityCache()


def _check(db_path: Path) -> tuple[dict[str, dict], list[list[dict]]] | None:
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
        return client.build_quality_index(), client.list_duplicate_groups()
    finally:
        client.close()


async def run_plex_quality_check_forever(
    db_path: Path, cache: PlexQualityCache, stop_event: asyncio.Event
) -> None:
    while not stop_event.is_set():
        try:
            result = await asyncio.wait_for(asyncio.to_thread(_check, db_path), timeout=CHECK_TIMEOUT_SECONDS)
            if result is not None:
                cache.index, cache.duplicate_groups = result
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
