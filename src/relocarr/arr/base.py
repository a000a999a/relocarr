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


@dataclass
class DownloadClientSettings:
    name: str
    remove_completed_downloads: bool


@dataclass
class NotificationSettings:
    name: str
    implementation: str
    any_trigger_enabled: bool


@dataclass
class QualityProfile:
    id: int
    name: str


@dataclass
class MovieMatch:
    """A Radarr movie, however we found it -- by its current file's path,
    or by title/year for one relocarr (or the user) has already unmonitored
    and whose file is gone. See RadarrApp.build_movie_index.
    """

    movie_id: int
    movie_file_id: int | None
    quality_profile_id: int
    title: str
    monitored: bool
    has_file: bool


@dataclass
class Release:
    """One candidate release from Radarr's interactive search
    (GET /api/v3/release) -- the same list the Radarr UI's manual search
    screen shows, before anything is grabbed. See
    RadarrApp.find_smaller_release.
    """

    guid: str
    indexer_id: int
    size_bytes: int
    title: str


@dataclass
class PreflightData:
    """Raw settings the Section 10 preflight checklist is evaluated
    against. One real API call per field, all confirmed live against
    Radarr 6.4.4 / Sonarr during this session's manual walkthrough and
    spike testing -- see arr/preflight.py for how these map to checks.
    """

    root_folders: list[RootFolder]
    completed_download_handling_enabled: bool
    download_clients: list[DownloadClientSettings]
    recycle_bin: str
    use_hardlinks: bool
    rename_enabled: bool
    import_extra_files: bool
    extra_file_extensions: list[str]
    notifications: list[NotificationSettings]
    webhook_configured: bool


class ArrApp(Protocol):
    """Interface implemented by arr/radarr.py and arr/sonarr.py."""

    def test_connection(self) -> ConnectionTestResult: ...

    def list_root_folders(self) -> list[RootFolder]: ...

    def get_preflight_data(self) -> PreflightData:
        """Section 10: the raw settings the preflight page checks."""
        ...

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
