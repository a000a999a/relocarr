from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

from ..arr.base import ArrApp
from ..clients.base import DownloadClient
from .gate import removal_gate_open
from .machine import advance_clean, advance_copy, advance_reconcile

logger = logging.getLogger(__name__)


def run_once(
    conn: sqlite3.Connection,
    *,
    app_id: int,
    arr_client: ArrApp,
    download_client: DownloadClient | None,
    sidecar_extensions: list[str],
    verify_mode: str,
    settle_window_seconds: float = 30.0,
    dry_run: bool = False,
) -> None:
    """One orchestration pass over this app's jobs: advances every eligible
    job exactly one step. Does not loop internally -- a scheduled background
    task (not yet wired into main.py) decides polling cadence, per the
    Settings page's configurable poll interval (Section 9).
    """
    # next_attempt_at gates a job that just took a transient failure --
    # Section 12's exponential backoff, computed and set by
    # jobs/machine.py's _schedule_retry. NULL means "attempt immediately"
    # (a fresh IMPORTED job, or one that never failed).
    for job in conn.execute(
        """SELECT * FROM jobs WHERE app_id = ? AND state IN ('IMPORTED', 'COPYING')
           AND (next_attempt_at IS NULL OR next_attempt_at <= strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))""",
        (app_id,),
    ).fetchall():
        advance_copy(
            conn, job, sidecar_extensions=sidecar_extensions, verify_mode=verify_mode, dry_run=dry_run
        )

    for job in conn.execute(
        "SELECT * FROM jobs WHERE app_id = ? AND state = 'PUBLISHED'", (app_id,)
    ).fetchall():
        advance_reconcile(conn, job, arr_client, dry_run=dry_run)

    _advance_removal_gate(
        conn,
        app_id=app_id,
        arr_client=arr_client,
        download_client=download_client,
        settle_window_seconds=settle_window_seconds,
        dry_run=dry_run,
    )


def _advance_removal_gate(
    conn: sqlite3.Connection,
    *,
    app_id: int,
    arr_client: ArrApp,
    download_client: DownloadClient | None,
    settle_window_seconds: float,
    dry_run: bool = False,
) -> None:
    # No downloadId (manual imports, spike 2 finding) -> no torrent to
    # correlate against or remove; skip the gate entirely.
    for job in conn.execute(
        "SELECT * FROM jobs WHERE app_id = ? AND state = 'RECONCILED' AND download_id IS NULL",
        (app_id,),
    ).fetchall():
        advance_clean(conn, job, dry_run=dry_run)

    if download_client is None:
        return

    download_ids = [
        row["download_id"]
        for row in conn.execute(
            """SELECT DISTINCT download_id FROM jobs
               WHERE app_id = ? AND download_id IS NOT NULL AND state NOT IN ('CLEANED', 'DONE')""",
            (app_id,),
        ).fetchall()
    ]
    if not download_ids:
        return

    queue_entries = arr_client.get_queue()
    torrents_by_hash = {t.hash.lower(): t for t in download_client.list_torrents()}

    for download_id in download_ids:
        siblings = conn.execute(
            "SELECT * FROM jobs WHERE app_id = ? AND download_id = ? COLLATE NOCASE",
            (app_id, download_id),
        ).fetchall()

        last_seen = max(job["created_at"] for job in siblings)
        result = removal_gate_open(
            download_id=download_id,
            sibling_job_states=[job["state"] for job in siblings],
            queue_entries=queue_entries,
            seconds_since_last_webhook=_seconds_since(last_seen),
            settle_window_seconds=settle_window_seconds,
        )
        if not result.open:
            continue

        torrent = torrents_by_hash.get(download_id.lower())
        if torrent is not None:
            if dry_run:
                logger.info("[DRY RUN] would remove torrent %s (%s)", torrent.id, torrent.name)
            else:
                download_client.remove(torrent.id, delete_data=True)

        for job in siblings:
            if job["state"] == "RECONCILED":
                advance_clean(conn, job, dry_run=dry_run)


def _seconds_since(iso_timestamp: str) -> float:
    then = datetime.strptime(iso_timestamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds()
