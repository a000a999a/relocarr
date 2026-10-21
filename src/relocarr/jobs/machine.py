from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from ..arr.base import ArrApp
from ..transfer.copier import rsync_copy, verify
from ..transfer.health import probe_destination
from ..transfer.layout import sidecar_paths

logger = logging.getLogger(__name__)

BACKOFF_CAP_MINUTES = 30.0


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


def compute_backoff_minutes(retry_count: int, *, cap_minutes: float = BACKOFF_CAP_MINUTES) -> float:
    """Section 12: "jobs wait with exponential backoff (capped, e.g. 30
    min)". retry_count=1 -> 2 min, 2 -> 4, 3 -> 8, ... capped.
    """
    return min(2.0**retry_count, cap_minutes)


def _schedule_retry(conn: sqlite3.Connection, job: sqlite3.Row, *, error: str) -> None:
    # Stays in COPYING (not reset to IMPORTED) -- the worker's query only
    # picks this job up again once next_attempt_at has passed, so the next
    # actual attempt re-probes the destination and re-runs rsync (which
    # resumes via --partial rather than restarting).
    retry_count = job["retry_count"] + 1
    backoff_minutes = max(1, round(compute_backoff_minutes(retry_count)))
    conn.execute(
        """UPDATE jobs SET state = 'COPYING', error = ?, retry_count = ?,
           next_attempt_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?),
           updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
           WHERE id = ?""",
        (error, retry_count, f"+{backoff_minutes} minutes", job["id"]),
    )
    conn.commit()


def advance_copy(
    conn: sqlite3.Connection,
    job: sqlite3.Row,
    *,
    sidecar_extensions: list[str],
    verify_mode: str,
    dry_run: bool = False,
) -> None:
    """IMPORTED/COPYING -> VERIFIED -> PUBLISHED, or NEEDS_ATTENTION on a
    permanent failure. A transient failure (including a failed destination
    health probe, run first, before rsync even starts -- Section 12) leaves
    the job in COPYING with an exponential backoff (compute_backoff_minutes)
    before the worker's query will pick it up again; the retry resumes via
    rsync's --partial rather than restarting.

    dry_run (Section 12: "logs every action it would take and performs
    none"): logs what would happen and returns immediately -- no rsync
    call, no state change, so the job stays exactly where it is for the
    next tick to evaluate again. This is deliberately NOT the same as
    actually running the step and discarding the result; a real rsync
    attempt could still touch the destination (create its temp file) even
    on failure, which dry-run must never risk.
    """
    source = Path(job["source_path"])
    target = Path(job["target_path"])

    if dry_run:
        logger.info("[DRY RUN] would copy %s -> %s", source, target)
        return

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _schedule_retry(conn, job, error=f"could not create destination folder: {exc}")
        return

    probe = probe_destination(target.parent)
    if not probe.ok:
        _schedule_retry(conn, job, error=f"destination health probe failed: {probe.error}")
        return

    _set_state(conn, job["id"], "COPYING")
    result = rsync_copy(source, target)
    if not result.ok:
        if result.transient:
            _schedule_retry(conn, job, error=result.error)
            return
        _set_state(conn, job["id"], "NEEDS_ATTENTION", error=result.error)
        return

    conn.execute(
        "UPDATE jobs SET retry_count = 0, next_attempt_at = NULL WHERE id = ?", (job["id"],)
    )
    conn.commit()

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


def advance_reconcile(
    conn: sqlite3.Connection, job: sqlite3.Row, arr_client: ArrApp, *, dry_run: bool = False
) -> None:
    """PUBLISHED -> RECONCILED: delete the file through the *arr's own API
    (so its database stays consistent) and unmonitor so it's never
    re-downloaded. PLAN.md Section 5/6.
    """
    if dry_run:
        logger.info(
            "[DRY RUN] would delete_file(%s) and unmonitor(%s)",
            job["media_file_id"],
            job["unmonitor_id"],
        )
        return

    try:
        arr_client.delete_file(job["media_file_id"])
        arr_client.unmonitor(job["unmonitor_id"])
    except Exception as exc:  # noqa: BLE001 -- arr API failures are all "retry later" for now
        _set_state(conn, job["id"], "NEEDS_ATTENTION", error=str(exc))
        return
    _set_state(conn, job["id"], "RECONCILED")


def advance_clean(conn: sqlite3.Connection, job: sqlite3.Row, *, dry_run: bool = False) -> None:
    """RECONCILED -> CLEANED: prune the now-empty per-movie/episode staging
    folder. Confirmed via spike 8 (2026-10-21) that no *arr setting does
    this -- it's entirely relocarr's own responsibility.
    """
    if dry_run:
        logger.info("[DRY RUN] would prune %s if empty", Path(job["source_path"]).parent)
        return

    folder = Path(job["source_path"]).parent
    try:
        if folder.exists() and not any(folder.iterdir()):
            folder.rmdir()
    except OSError:
        pass  # not empty (a sibling job's file still lives there) or already gone -- fine either way
    _set_state(conn, job["id"], "CLEANED")
    _set_state(conn, job["id"], "DONE")
