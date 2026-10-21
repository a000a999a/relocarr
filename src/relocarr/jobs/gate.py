from __future__ import annotations

from dataclasses import dataclass

from ..arr.base import QueueEntry

TERMINAL_JOB_STATES = {"RECONCILED", "CLEANED", "DONE"}
QUEUE_BLOCKING_STATES = {"importPending", "importBlocked", "importFailed"}

DEFAULT_SETTLE_WINDOW_SECONDS = 30.0


@dataclass
class GateResult:
    open: bool
    reason: str


def removal_gate_open(
    *,
    download_id: str,
    sibling_job_states: list[str],
    queue_entries: list[QueueEntry],
    seconds_since_last_webhook: float,
    settle_window_seconds: float = DEFAULT_SETTLE_WINDOW_SECONDS,
) -> GateResult:
    """PLAN.md Section 5: remove the torrent only when every job sharing this
    downloadId (season packs, multi-movie torrents) is reconciled, the *arr
    queue agrees, and a settle window has passed with no new webhook.

    Condition (b) is checked as "no matching queue entry" rather than
    "entry present and specifically == imported": spike 5 (2026-10-21, real
    grab, polled every 20s by two independent methods) found the import step
    completes *inside a single poll window* -- a transient "imported" state
    was never actually observed, only "downloading" then gone. Treating
    presence-with-an-unknown-state as "not yet gone" is the conservative
    reading: it only blocks removal a little longer, never removes early.
    """
    if not sibling_job_states:
        return GateResult(False, "no jobs found for this downloadId")

    if not all(state in TERMINAL_JOB_STATES for state in sibling_job_states):
        return GateResult(False, "not all jobs for this downloadId are reconciled yet")

    matching = [
        q for q in queue_entries if q.download_id.lower() == download_id.lower()
    ]
    if matching:
        state = matching[0].tracked_download_state
        if state in QUEUE_BLOCKING_STATES:
            return GateResult(False, f"queue still reports {state}")
        return GateResult(False, f"queue entry still present (state={state})")

    if seconds_since_last_webhook < settle_window_seconds:
        return GateResult(False, "settle window has not elapsed")

    return GateResult(True, "gate open")
