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
) -> None:
    """One app's worth of a scheduler tick: seal (Section 4 phase A), then
    advance every job one step (Section 5). Kept separate from `_tick` below
    so it's directly testable with injected fakes, same pattern as
    jobs/worker.py's tests.
    """
    if download_client is not None:
        seal_completed_downloads(
            conn,
            app_id=app["id"],
            arr_client=arr_client,
            download_client=download_client,
            dry_run=dry_run,
        )
    run_once(
        conn,
        app_id=app["id"],
        arr_client=arr_client,
        download_client=download_client,
        sidecar_extensions=sidecar_extensions,
        verify_mode=verify_mode,
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

    for app in apps:
        arr_client = arr.make_app(app["kind"], app["base_url"], app["api_key"])
        download_client: DownloadClient | None = None
        try:
            with session(db_path) as conn:
                dc_row = conn.execute(
                    "SELECT * FROM download_clients WHERE app_id = ? LIMIT 1", (app["id"],)
                ).fetchone()
            if dc_row is not None and dc_row["kind"] == "transmission":
                download_client = TransmissionClient(
                    dc_row["host"], dc_row["port"], dc_row["username"], dc_row["password"]
                )

            with session(db_path) as conn:
                run_app_tick(
                    conn,
                    app,
                    arr_client=arr_client,
                    download_client=download_client,
                    sidecar_extensions=sidecar_extensions,
                    verify_mode=verify_mode,
                    dry_run=dry_run,
                )
        except Exception:  # noqa: BLE001 -- one app's failure must not stop the others
            logger.exception("scheduler tick failed for app %r", app["name"])
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
