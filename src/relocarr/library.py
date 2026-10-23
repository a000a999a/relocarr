from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

MOVIE_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".wmv", ".ts", ".m2ts", ".webm"}

SCAN_INTERVAL_SECONDS = 600.0


def scan_movie_files(apps: list[sqlite3.Row]) -> list[dict]:
    """Walk every configured Radarr target folder and return each movie
    file's name, owning app, and size. A real filesystem read, not a query
    over job history -- relocarr's job log only has entries for files it
    personally moved, a small fraction of a library that likely existed
    before relocarr did.
    """
    files: list[dict] = []
    seen_roots: set[str] = set()
    for app in apps:
        if app["kind"] != "radarr":
            continue
        target_root = app["target_root"]
        if not target_root or target_root in seen_roots:
            continue
        seen_roots.add(target_root)
        root = Path(target_root)
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.suffix.lower() not in MOVIE_EXTENSIONS:
                continue
            try:
                if not path.is_file():
                    continue
                size_bytes = path.stat().st_size
            except OSError:
                continue
            files.append({"name": path.name, "app_name": app["name"], "size_bytes": size_bytes})
    return files


class MovieLibraryCache:
    """A real scan over a network-mounted library can take tens of seconds
    (one stat() round trip per file) -- far too slow to run inline in a web
    request. A slow background loop refreshes this instead; the Log page
    reads whatever's here and shows "scanning..." before the first scan
    completes.
    """

    def __init__(self) -> None:
        self.files: list[dict] = []
        self.scanned_at: float | None = None


cache = MovieLibraryCache()


async def run_library_scan_forever(
    db_path: Path, cache: MovieLibraryCache, stop_event: asyncio.Event
) -> None:
    from .db import session

    while not stop_event.is_set():
        try:
            with session(db_path) as conn:
                apps = conn.execute("SELECT * FROM apps").fetchall()
            files = await asyncio.to_thread(scan_movie_files, apps)
            cache.files = files
            cache.scanned_at = time.time()
        except Exception:  # noqa: BLE001 -- a failed scan must not kill the loop
            logger.exception("movie library scan failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=SCAN_INTERVAL_SECONDS)
        except TimeoutError:
            pass
