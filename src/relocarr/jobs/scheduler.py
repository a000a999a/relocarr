from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path

from .. import arr
from ..arr.base import ArrApp
from ..clients.base import DownloadClient
from ..clients.transmission import TransmissionClient
from ..clients.watcher import seal_completed_downloads
from ..db import session
from .worker import run_once

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_SECONDS = 15.0


def run_app_tick(
    conn: sqlite3.Connection,
    app: sqlite3.Row,
    *,
    arr_client: ArrApp,
    download_client: DownloadClient | None,
    sidecar_extensions: list[str],
    verify_mode: str,
    dry_run: bool = False,
    bwlimit_mbps: int | None = None,
) -> None:
    """One app's worth of a job-processing tick: advance every job one step
    (Section 5). Sealing (Section 4 phase A) used to run as the first step
    of this same call -- split out to `run_app_seal_tick` below and its own
    independent loop, 2026-10-28, after a real incident: once rsync copies
    started being throttled (`--bwlimit`, the Shield-OOM fix), a backlog of
    a few large files could keep this call blocked for several minutes
    doing nothing but copying, during which a completely different,
    already-completed torrent sat seeding -- unsealed -- because sealing
    could only ever run between two full passes of this call. Kept separate
    from `_tick` below so it's directly testable with injected fakes, same
    pattern as jobs/worker.py's tests.

    `bwlimit_mbps` is global (Settings -> General), not per-app -- it
    protects whatever's actually receiving the writes, not the *arr app
    driving them, and a user may point several apps at the same
    RAM-constrained destination. See `_bwlimit_mbps`/copier.py's
    `rsync_copy` for where the value comes from and why it exists.
    """
    run_once(
        conn,
        app_id=app["id"],
        arr_client=arr_client,
        download_client=download_client,
        sidecar_extensions=sidecar_extensions,
        verify_mode=verify_mode,
        dry_run=dry_run,
        bwlimit_mbps=bwlimit_mbps,
    )


def run_app_seal_tick(
    conn: sqlite3.Connection,
    app: sqlite3.Row,
    *,
    arr_client: ArrApp,
    download_client: DownloadClient | None,
    dry_run: bool = False,
) -> None:
    """One app's worth of the seal loop (Section 4 phase A) -- see
    run_app_tick's docstring for why this is split out onto its own
    independent cadence rather than sharing a tick with job processing.
    """
    if download_client is not None:
        seal_completed_downloads(
            conn,
            app_id=app["id"],
            arr_client=arr_client,
            download_client=download_client,
            dry_run=dry_run,
        )


def _poll_interval(db_path: Path) -> float:
    with session(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'poll_interval_seconds'"
        ).fetchone()
    try:
        return float(row["value"]) if row else DEFAULT_POLL_INTERVAL_SECONDS
    except (TypeError, ValueError):
        return DEFAULT_POLL_INTERVAL_SECONDS


def _bwlimit_mbps(settings_rows: dict[str, str]) -> int | None:
    """Parses Settings -> General's rsync throttle override. Not validated
    at write time (same as poll_interval_seconds) -- validated here
    instead, every read, so a bad/missing/non-positive value can never
    reach rsync_copy as a literal "0" (which rsync treats as *uncapped*,
    the exact OOM risk this setting exists to prevent) -- falls back to
    None, which copier.py's own default (10 MB/s) then applies.
    """
    try:
        value = int(settings_rows.get("bwlimit_mbps", ""))
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None


def _build_download_client(db_path: Path, app_id: int) -> DownloadClient | None:
    with session(db_path) as conn:
        dc_row = conn.execute(
            "SELECT * FROM download_clients WHERE app_id = ? LIMIT 1", (app_id,)
        ).fetchone()
    if dc_row is not None and dc_row["kind"] == "transmission":
        return TransmissionClient(dc_row["host"], dc_row["port"], dc_row["username"], dc_row["password"])
    return None


def _tick(db_path: Path) -> None:
    with session(db_path) as conn:
        apps = conn.execute("SELECT * FROM apps").fetchall()
        settings_rows = {
            r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings").fetchall()
        }

    sidecar_extensions = [
        ext.strip() for ext in settings_rows.get("sidecar_extensions", "srt").split(",") if ext.strip()
    ]
    verify_mode = settings_rows.get("verify_mode", "size")
    dry_run = settings_rows.get("dry_run", "true") == "true"
    bwlimit_mbps = _bwlimit_mbps(settings_rows)

    for app in apps:
        arr_client = arr.make_app(app["kind"], app["base_url"], app["api_key"])
        download_client: DownloadClient | None = None
        try:
            download_client = _build_download_client(db_path, app["id"])

            with session(db_path) as conn:
                run_app_tick(
                    conn,
                    app,
                    arr_client=arr_client,
                    download_client=download_client,
                    sidecar_extensions=sidecar_extensions,
                    verify_mode=verify_mode,
                    dry_run=dry_run,
                    bwlimit_mbps=bwlimit_mbps,
                )
        except Exception:  # noqa: BLE001 -- one app's failure must not stop the others
            logger.exception("scheduler tick failed for app %r", app["name"])
        finally:
            arr_client.close()
            if download_client is not None:
                download_client.close()


def _seal_tick(db_path: Path) -> None:
    """Independent of _tick above -- see run_app_seal_tick's docstring."""
    with session(db_path) as conn:
        apps = conn.execute("SELECT * FROM apps").fetchall()
        dry_run_row = conn.execute("SELECT value FROM settings WHERE key = 'dry_run'").fetchone()
    dry_run = (dry_run_row["value"] if dry_run_row else "true") == "true"

    for app in apps:
        arr_client = arr.make_app(app["kind"], app["base_url"], app["api_key"])
        download_client: DownloadClient | None = None
        try:
            download_client = _build_download_client(db_path, app["id"])
            if download_client is None:
                continue

            with session(db_path) as conn:
                run_app_seal_tick(
                    conn,
                    app,
                    arr_client=arr_client,
                    download_client=download_client,
                    dry_run=dry_run,
                )
        except Exception:  # noqa: BLE001 -- one app's failure must not stop the others
            logger.exception("seal tick failed for app %r", app["name"])
        finally:
            arr_client.close()
            if download_client is not None:
                download_client.close()


async def run_forever(db_path: Path, stop_event: asyncio.Event) -> None:
    """Started from main.py's lifespan, stopped on shutdown. Each tick runs
    in a thread (httpx/sqlite3 calls here are all synchronous) so it never
    blocks the event loop serving web requests.
    """
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(_tick, db_path)
        except Exception:  # noqa: BLE001
            logger.exception("scheduler tick crashed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_poll_interval(db_path))
        except TimeoutError:
            pass


async def run_seal_forever(db_path: Path, stop_event: asyncio.Event) -> None:
    """Runs alongside run_forever, independently, on the same configured
    poll interval -- see run_app_seal_tick's docstring for why this can't
    share a tick with job processing. Safe to run concurrently with
    run_forever: everything here is either a lightweight Radarr/Sonarr/
    Transmission API call or its own sqlite3 connection (relocarr already
    runs several such independent background loops at once -- library
    scan, disk health, Plex quality check -- this is the same pattern),
    never a second simultaneous large file write, which is the actual risk
    the single-copy-at-a-time design (PLAN.md, confirmed live 2026-10-24)
    exists to avoid.
    """
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(_seal_tick, db_path)
        except Exception:  # noqa: BLE001
            logger.exception("seal loop crashed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_poll_interval(db_path))
        except TimeoutError:
            pass
