from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

MOVIE_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".wmv", ".ts", ".m2ts", ".webm"}

SCAN_INTERVAL_SECONDS = 600.0

# A real scan takes ~20s even over a slow network mount (measured). 60s gives
# generous headroom for "just slow" without leaving a wedged mount (ping
# works, I/O hangs in uninterruptible D-state -- a real, documented failure
# mode for this project's NAS) able to freeze the cache forever. Note this
# can't actually kill the underlying blocked thread -- Python can't cancel a
# blocked syscall -- it just stops *waiting* on it, so the loop keeps retrying
# on schedule instead of getting stuck. A wedge still leaks one stuck thread
# per retry until the mount recovers; this bounds the damage, it doesn't
# eliminate the underlying OS-level issue.
SCAN_TIMEOUT_SECONDS = 60.0

_TITLE_YEAR_RE = re.compile(r"^(.*?)\s*\((\d{4})\)\s*$")


def parse_title_year(filename: str) -> tuple[str, int] | None:
    """Pulls "Title" and 1982 out of "Title (1982).mkv" -- Radarr's own
    flat-layout naming convention, confirmed against the real library
    (e.g. "Example Feature (1982).mkv"). Returns None for
    anything that doesn't match, rather than guessing.
    """
    stem = Path(filename).stem
    m = _TITLE_YEAR_RE.match(stem)
    if not m:
        return None
    return m.group(1).strip(), int(m.group(2))


def scan_movie_files(apps: list[sqlite3.Row]) -> tuple[list[dict], list[str]]:
    """Walk every configured Radarr target folder and return each movie
    file's name, owning app, and size, plus any target root that wasn't
    even reachable (e.g. the Shield powered off, or an autofs mount that
    never triggered) -- the System page's health badge surfaces that
    second part. A real filesystem read, not a query over job history --
    relocarr's job log only has entries for files it personally moved, a
    small fraction of a library that likely existed before relocarr did.
    """
    files: list[dict] = []
    unreachable_roots: list[str] = []
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
            unreachable_roots.append(target_root)
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
            files.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "app_id": app["id"],
                    "app_name": app["name"],
                    "size_bytes": size_bytes,
                }
            )
    return files, unreachable_roots


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
        self.unreachable_roots: list[str] = []
        self.last_timeout_at: float | None = None


cache = MovieLibraryCache()


async def run_library_scan_forever(
    db_path: Path, cache: MovieLibraryCache, stop_event: asyncio.Event
) -> None:
    from .db import session

    while not stop_event.is_set():
        try:
            with session(db_path) as conn:
                apps = conn.execute("SELECT * FROM apps").fetchall()
            try:
                files, unreachable_roots = await asyncio.wait_for(
                    asyncio.to_thread(scan_movie_files, apps), timeout=SCAN_TIMEOUT_SECONDS
                )
            except TimeoutError:
                logger.warning(
                    "movie library scan timed out after %ss -- possible wedged mount; "
                    "keeping the last good snapshot and retrying next cycle",
                    SCAN_TIMEOUT_SECONDS,
                )
                cache.last_timeout_at = time.time()
            else:
                cache.files = files
                cache.unreachable_roots = unreachable_roots
                cache.scanned_at = time.time()
        except Exception:  # noqa: BLE001 -- a failed scan must not kill the loop
            logger.exception("movie library scan failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=SCAN_INTERVAL_SECONDS)
        except TimeoutError:
            pass
