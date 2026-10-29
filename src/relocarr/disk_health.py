from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 60.0
# Generous for a healthy mount (a plain disk_usage() call is normally
# near-instant, nothing like the movie library's full directory walk), short
# enough that a wedged one never leaves a web request waiting long.
CHECK_TIMEOUT_SECONDS = 15.0

# Folder usage (how many bytes staging/target actually hold, as opposed to
# the whole mount's free/total) needs a full recursive walk -- the same cost
# as library.py's movie scan, not the near-instant disk_usage() call above.
# Confirmed live, 2026-10-29: the user only wants this as a once-a-day
# figure, not tracked continuously like free space is, so this runs on its
# own much slower interval rather than piggybacking on CHECK_INTERVAL_SECONDS.
FOLDER_SCAN_INTERVAL_SECONDS = 86400.0
FOLDER_SCAN_TIMEOUT_SECONDS = 300.0


def app_paths(apps) -> list[str]:
    """Every distinct, non-blank staging_root/target_root across the given
    apps rows, in first-seen order. Shared by both background loops below
    so they always check the same set of paths the System page itself
    lists.
    """
    paths: list[str] = []
    for app in apps:
        for p in (app["staging_root"], app["target_root"]):
            if p and p not in paths:
                paths.append(p)
    return paths


def check_disk_space(paths: list[str]) -> dict[str, dict]:
    """shutil.disk_usage() for each path, independently caught -- one
    unreachable mount must not stop the others from being checked. Also
    records each path's st_dev, so a caller can tell staging and target
    apart from "same filesystem, different subfolder" without its own
    (possibly mount-hanging) stat call -- confirmed live, 2026-10-29: two
    apps sharing one physical disk under different subfolders return
    identical free/total figures from disk_usage(), so summing every row
    naively double-counts that disk.
    """
    result: dict[str, dict] = {}
    for path in paths:
        if not path or path in result:
            continue
        try:
            usage = shutil.disk_usage(path)
            dev = os.stat(path).st_dev
            result[path] = {"free": usage.free, "total": usage.total, "dev": dev, "ok": True}
        except OSError as exc:
            result[path] = {"ok": False, "error": str(exc)}
    return result


def compute_folder_usage(paths: list[str]) -> dict[str, dict]:
    """Recursively sums file sizes actually sitting under each path -- the
    real content size of that specific staging/target folder, not the
    whole mount it happens to live on. One bad/unreachable path is caught
    independently, same as check_disk_space.
    """
    result: dict[str, dict] = {}
    for path in paths:
        if not path or path in result:
            continue
        root = Path(path)
        if not root.is_dir():
            result[path] = {"ok": False, "error": "does not exist or is not a directory"}
            continue
        used = 0
        try:
            for entry in root.rglob("*"):
                try:
                    if entry.is_file():
                        used += entry.stat().st_size
                except OSError:
                    continue
        except OSError as exc:
            result[path] = {"ok": False, "error": str(exc)}
            continue
        result[path] = {"used": used, "ok": True}
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
            paths = app_paths(apps)
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


class FolderUsageCache:
    """Same read-only-cache-plus-timeout shape as DiskSpaceCache, for the
    same reason -- compute_folder_usage() walks the real staging/target
    folders (a network mount for target), so it never runs inline in a web
    request either.
    """

    def __init__(self) -> None:
        self.entries: dict[str, dict] = {}
        self.checked_at: float | None = None
        self.last_timeout_at: float | None = None


folder_usage_cache = FolderUsageCache()


async def run_folder_usage_scan_forever(
    db_path: Path, cache: FolderUsageCache, stop_event: asyncio.Event
) -> None:
    from .db import session

    while not stop_event.is_set():
        try:
            with session(db_path) as conn:
                apps = conn.execute("SELECT * FROM apps").fetchall()
            paths = app_paths(apps)
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(compute_folder_usage, paths), timeout=FOLDER_SCAN_TIMEOUT_SECONDS
                )
            except TimeoutError:
                logger.warning(
                    "folder usage scan timed out after %ss -- possible wedged mount; "
                    "keeping the last good snapshot and retrying next cycle",
                    FOLDER_SCAN_TIMEOUT_SECONDS,
                )
                cache.last_timeout_at = time.time()
            else:
                cache.entries = result
                cache.checked_at = time.time()
        except Exception:  # noqa: BLE001 -- a failed scan must not kill the loop
            logger.exception("folder usage scan failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=FOLDER_SCAN_INTERVAL_SECONDS)
        except TimeoutError:
            pass
