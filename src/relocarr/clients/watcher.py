from __future__ import annotations

import sqlite3

from ..arr.base import ArrApp
from .base import DownloadClient


def seal_completed_downloads(
    conn: sqlite3.Connection,
    *,
    app_id: int,
    arr_client: ArrApp,
    download_client: DownloadClient,
) -> list[str]:
    """PLAN.md Section 4/5: the SEALED step, phase A of the two-phase
    design -- stop a torrent the moment it's complete, before the *arr has
    even imported it. Records one `downloads` row per (app, downloadId)
    before any job exists, and only once the stop is confirmed (Section 5:
    "Stop torrent; re-read and confirm it is stopped").

    Only acts on torrents whose hash appears in the *arr's current queue
    (Section 4 point 6) -- never touches a torrent this app isn't tracking,
    e.g. someone else's download on a shared Transmission instance.

    Known race, not fully solved for v0.1: spike 5 (2026-10-21) found the
    *arr can import a completed torrent fast enough that its queue entry is
    already gone by the next poll. If that happens before this runs, the
    torrent never gets explicitly sealed here -- it usually still imports
    fine regardless (spike 1: a stopped-or-not torrent imports the same
    way), this just means relocarr didn't get to stop it first.
    """
    queue_hashes = {entry.download_id.lower() for entry in arr_client.get_queue()}
    sealed: list[str] = []

    for torrent in download_client.list_torrents():
        if not torrent.is_complete:
            continue
        if torrent.hash.lower() not in queue_hashes:
            continue

        already_sealed = conn.execute(
            "SELECT 1 FROM downloads WHERE app_id = ? AND download_id = ? COLLATE NOCASE",
            (app_id, torrent.hash),
        ).fetchone()
        if already_sealed:
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
