from __future__ import annotations

import sqlite3
from pathlib import Path

from ..arr.base import ArrApp
from ..transfer.copier import rsync_copy, verify
from ..transfer.layout import sidecar_paths


def _set_state(conn: sqlite3.Connection, job_id: int, state: str, error: str | None = None) -> None:
    # Every transition persisted before any further action is taken on it --
    # PLAN.md Section 5's restart-safety requirement. Callers call this BEFORE
    # performing the step it names, so a crash mid-step resumes correctly:
    # a job found in COPYING on restart means "try the copy again", which is
    # safe because rsync (Section 12) resumes partial transfers itself.
    conn.execute(
        "UPDATE jobs SET state = ?, error = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
        (state, error, job_id),
    )
    conn.commit()


def advance_copy(conn: sqlite3.Connection, job: sqlite3.Row, *, sidecar_extensions: list[str], verify_mode: str) -> None:
    """IMPORTED/COPYING -> VERIFIED -> PUBLISHED, or NEEDS_ATTENTION on a
    permanent failure. A transient failure leaves the job in COPYING for the
    worker to retry later -- rsync's --partial means the retry resumes.
    """
    source = Path(job["source_path"])
    target = Path(job["target_path"])

    _set_state(conn, job["id"], "COPYING")
    result = rsync_copy(source, target)
    if not result.ok:
        if result.transient:
            return
        _set_state(conn, job["id"], "NEEDS_ATTENTION", error=result.error)
        return

    for sidecar in sidecar_paths(source, sidecar_extensions):
        sidecar_target = target.with_name(sidecar.name)
        sidecar_result = rsync_copy(sidecar, sidecar_target)
        if not sidecar_result.ok and not sidecar_result.transient:
            # A sidecar is a nice-to-have, not the media file itself -- log
            # and continue rather than blocking the whole job on a subtitle.
            pass

    _set_state(conn, job["id"], "VERIFIED")
    check = verify(source, target, mode=verify_mode)
    if not check.ok:
        _set_state(conn, job["id"], "NEEDS_ATTENTION", error=check.error)
        return

    _set_state(conn, job["id"], "PUBLISHED")
    # TODO notify/plex.py: notify the media server here once built.


def advance_reconcile(conn: sqlite3.Connection, job: sqlite3.Row, arr_client: ArrApp) -> None:
    """PUBLISHED -> RECONCILED: delete the file through the *arr's own API
    (so its database stays consistent) and unmonitor so it's never
    re-downloaded. PLAN.md Section 5/6.
    """
    try:
        arr_client.delete_file(job["media_file_id"])
        arr_client.unmonitor(job["unmonitor_id"])
    except Exception as exc:  # noqa: BLE001 -- arr API failures are all "retry later" for now
        _set_state(conn, job["id"], "NEEDS_ATTENTION", error=str(exc))
        return
    _set_state(conn, job["id"], "RECONCILED")


def advance_clean(conn: sqlite3.Connection, job: sqlite3.Row) -> None:
    """RECONCILED -> CLEANED: prune the now-empty per-movie/episode staging
    folder. Confirmed via spike 8 (2026-10-21) that no *arr setting does
    this -- it's entirely relocarr's own responsibility.
    """
    folder = Path(job["source_path"]).parent
    try:
        if folder.exists() and not any(folder.iterdir()):
            folder.rmdir()
    except OSError:
        pass  # not empty (a sibling job's file still lives there) or already gone -- fine either way
    _set_state(conn, job["id"], "CLEANED")
    _set_state(conn, job["id"], "DONE")
