from relocarr.arr.base import QueueEntry
from relocarr.jobs.gate import removal_gate_open


def test_closed_with_no_jobs():
    result = removal_gate_open(
        download_id="abc",
        sibling_job_states=[],
        queue_entries=[],
        seconds_since_last_webhook=999,
    )
    assert not result.open


def test_closed_while_a_sibling_job_is_not_reconciled():
    # Season pack: one episode reconciled, one still copying.
    result = removal_gate_open(
        download_id="abc",
        sibling_job_states=["RECONCILED", "COPYING"],
        queue_entries=[],
        seconds_since_last_webhook=999,
    )
    assert not result.open
    assert "reconciled" in result.reason


def test_closed_while_queue_reports_import_blocked():
    result = removal_gate_open(
        download_id="ABC123",
        sibling_job_states=["RECONCILED"],
        queue_entries=[
            QueueEntry(download_id="abc123", tracked_download_state="importBlocked", tracked_download_status="warning")
        ],
        seconds_since_last_webhook=999,
    )
    assert not result.open
    assert "importBlocked" in result.reason


def test_closed_while_queue_entry_still_present_in_any_state():
    # spike 5 (2026-10-21): a transient "imported" state was never actually
    # observed -- presence in the queue at all is treated as "not gone yet".
    result = removal_gate_open(
        download_id="ABC123",
        sibling_job_states=["RECONCILED"],
        queue_entries=[
            QueueEntry(download_id="abc123", tracked_download_state="downloading", tracked_download_status="ok")
        ],
        seconds_since_last_webhook=999,
    )
    assert not result.open


def test_closed_before_settle_window_elapses():
    result = removal_gate_open(
        download_id="abc",
        sibling_job_states=["RECONCILED"],
        queue_entries=[],
        seconds_since_last_webhook=5,
        settle_window_seconds=30,
    )
    assert not result.open
    assert "settle" in result.reason


def test_open_when_all_conditions_met():
    result = removal_gate_open(
        download_id="abc",
        sibling_job_states=["RECONCILED", "CLEANED", "DONE"],
        queue_entries=[],
        seconds_since_last_webhook=60,
        settle_window_seconds=30,
    )
    assert result.open


def test_download_id_comparison_is_case_insensitive():
    # spike 4 (2026-10-21): Transmission's hash is lowercase, the *arr's
    # downloadId is uppercase, for the same torrent.
    result = removal_gate_open(
        download_id="0123456789abcdef0123456789abcdef01234567",
        sibling_job_states=["RECONCILED"],
        queue_entries=[
            QueueEntry(
                download_id="0123456789ABCDEF0123456789ABCDEF01234567",
                tracked_download_state="downloading",
                tracked_download_status="ok",
            )
        ],
        seconds_since_last_webhook=999,
    )
    # still blocked (entry present), proving the case-insensitive match found it
    assert not result.open
    assert "still present" in result.reason
