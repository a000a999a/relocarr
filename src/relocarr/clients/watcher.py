from __future__ import annotations

import logging
import sqlite3

from ..arr.base import ArrApp
from .base import DownloadClient

logger = logging.getLogger(__name__)


def seal_completed_downloads(
    conn: sqlite3.Connection,
    *,
    app_id: int,
    arr_client: ArrApp,
    download_client: DownloadClient,
    dry_run: bool = False,
) -> list[str]:
    """PLAN.md Section 4/5: the SEALED step, phase A of the two-phase
    design -- stop a torrent the moment it's complete, before the *arr has
    even imported it. Records one `downloads` row per (app, downloadId)
    before any job exists, and only once the stop is confirmed (Section 5:
    "Stop torrent; re-read and confirm it is stopped").

    Only acts on torrents whose hash has ever appeared in this app's *arr
    queue (Section 4 point 6) -- never touches a torrent this app isn't
    tracking, e.g. someone else's download on a shared Transmission
    instance. "Ever appeared" rather than "appears right now" is
    deliberate: fixed 2026-10-23, confirmed live (3 real completed
    downloads kept seeding indefinitely) -- Radarr can drop a torrent from
    its queue the moment it hits 100%, sometimes faster than the next poll,
    so checking only the current queue snapshot missed it permanently (it
    never got another chance to be sealed). Every hash seen in the queue is
    now recorded in `seen_downloads` on every poll, and sealing checks
    against that accumulated set instead of the live snapshot alone.
    """
    queue_hashes = {entry.download_id.lower() for entry in arr_client.get_queue()}
    for queue_hash in queue_hashes:
        conn.execute(
            "INSERT OR IGNORE INTO seen_downloads (app_id, download_id) VALUES (?, ?)",
            (app_id, queue_hash),
        )
    conn.commit()

    tracked_hashes = queue_hashes | {
        row["download_id"].lower()
        for row in conn.execute(
            "SELECT download_id FROM seen_downloads WHERE app_id = ?", (app_id,)
        ).fetchall()
    }

    sealed: list[str] = []

    for torrent in download_client.list_torrents():
        if not torrent.is_complete:
            continue
        if torrent.hash.lower() not in tracked_hashes:
            continue

        already_sealed = conn.execute(
            "SELECT 1 FROM downloads WHERE app_id = ? AND download_id = ? COLLATE NOCASE",
            (app_id, torrent.hash),
        ).fetchone()
        if already_sealed:
            continue

        if dry_run:
            logger.info("[DRY RUN] would stop torrent %s (%s)", torrent.id, torrent.name)
            continue

        download_client.stop(torrent.id)

        confirmed = any(
            t.id == torrent.id and t.is_stopped for t in download_client.list_torrents()
        )
        if not confirmed:
            continue  # try again next poll rather than recording SEALED prematurely

        conn.execute(
            "INSERT INTO downloads (app_id, download_id) VALUES (?, ?)",
            (app_id, torrent.hash),
        )
        conn.commit()
        sealed.append(torrent.hash)

    return sealed
