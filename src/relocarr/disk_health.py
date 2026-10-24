from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 60.0
# Generous for a healthy mount (a plain disk_usage() call is normally
# near-instant, nothing like the movie library's full directory walk), short
# enough that a wedged one never leaves a web request waiting long.
CHECK_TIMEOUT_SECONDS = 15.0


def check_disk_space(paths: list[str]) -> dict[str, dict]:
    """shutil.disk_usage() for each path, independently caught -- one
    unreachable mount must not stop the others from being checked.
    """
    result: dict[str, dict] = {}
    for path in paths:
        if not path or path in result:
            continue
        try:
            usage = shutil.disk_usage(path)
            result[path] = {"free": usage.free, "total": usage.total, "ok": True}
        except OSError as exc:
            result[path] = {"ok": False, "error": str(exc)}
    return result


class DiskSpaceCache:
    """Mirrors library.py's MovieLibraryCache, and exists for the same
    reason: shutil.disk_usage() on a wedged network mount can hang
    indefinitely -- confirmed live, 2026-10-24, when a Shield power-cycle
    left /log and /system both unresponsive for as long as the mount stayed
    stuck, because both called disk_usage() directly in the request handler
    with no timeout. This cache is read-only for every web request; a
    background loop with its own timeout is the only thing that ever
    actually calls disk_usage(), so a wedge can degrade this cache's
    freshness but can never again block a page from rendering.
    """

    def __init__(self) -> None:
        self.entries: dict[str, dict] = {}
        self.checked_at: float | None = None
        self.last_timeout_at: float | None = None


cache = DiskSpaceCache()


async def run_disk_space_check_forever(
    db_path: Path, cache: DiskSpaceCache, stop_event: asyncio.Event
) -> None:
    from .db import session

    while not stop_event.is_set():
        try:
            with session(db_path) as conn:
                apps = conn.execute("SELECT * FROM apps").fetchall()
            paths: list[str] = []
            for app in apps:
                for p in (app["staging_root"], app["target_root"]):
                    if p and p not in paths:
                        paths.append(p)
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(check_disk_space, paths), timeout=CHECK_TIMEOUT_SECONDS
                )
            except TimeoutError:
                logger.warning(
                    "disk space check timed out after %ss -- possible wedged mount; "
                    "keeping the last good snapshot and retrying next cycle",
                    CHECK_TIMEOUT_SECONDS,
                )
                cache.last_timeout_at = time.time()
            else:
                cache.entries = result
                cache.checked_at = time.time()
        except Exception:  # noqa: BLE001 -- a failed check must not kill the loop
            logger.exception("disk space check failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=CHECK_INTERVAL_SECONDS)
        except TimeoutError:
            pass
