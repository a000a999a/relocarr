from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class ConnectionTestResult:
    ok: bool
    version: str | None = None
    error: str | None = None


@dataclass
class RootFolder:
    id: int
    path: str


@dataclass
class QueueEntry:
    download_id: str
    tracked_download_state: str
    tracked_download_status: str


class ArrApp(Protocol):
    """Interface implemented by arr/radarr.py and arr/sonarr.py."""

    def test_connection(self) -> ConnectionTestResult: ...

    def list_root_folders(self) -> list[RootFolder]: ...

    def delete_file(self, file_id: str) -> None:
        """DELETE the tracked media file. PLAN.md Section 5, RECONCILED."""
        ...

    def unmonitor(self, media_id: int) -> None:
        """Unmonitor the movie/series so it is never re-downloaded."""
        ...

    def get_queue(self) -> list[QueueEntry]:
        """Current queue entries, keyed by downloadId. Used by the removal
        gate (jobs/gate.py) -- an entry absent from this list and confirmed
        imported in history means safe to remove (spike 5, 2026-10-21: the
        import step can complete inside a single poll window, so "gone from
        the queue" is the signal to trust, not a specific transient state).
        """
        ...
