from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from relocarr.arr.base import ConnectionTestResult, QueueEntry, RootFolder
from relocarr.clients.base import Torrent


class FakeArrApp:
    def __init__(
        self, queue: list[QueueEntry] | None = None, file_paths: dict[str, Path] | None = None
    ) -> None:
        # file_paths lets delete_file() actually unlink on disk, mirroring
        # what Radarr/Sonarr's real DELETE endpoint does (confirmed live,
        # spike 8, 2026-10-21) -- tests that care about folder-pruning
        # behavior need this; tests that don't can omit it.
        self.deleted_files: list[str] = []
        self.unmonitored: list[int] = []
        self._queue = queue or []
        self._file_paths = file_paths or {}
        self.fail_delete = False

    def test_connection(self) -> ConnectionTestResult:
        return ConnectionTestResult(ok=True, version="fake")

    def list_root_folders(self) -> list[RootFolder]:
        return []

    def delete_file(self, file_id: str) -> None:
        if self.fail_delete:
            raise RuntimeError("simulated arr API failure")
        self.deleted_files.append(file_id)
        path = self._file_paths.get(file_id)
        if path is not None and path.exists():
            path.unlink()

    def unmonitor(self, media_id: int) -> None:
        self.unmonitored.append(media_id)

    def get_queue(self) -> list[QueueEntry]:
        return self._queue


@dataclass
class FakeDownloadClient:
    torrents: list[Torrent] = field(default_factory=list)
    removed: list[tuple[int, bool]] = field(default_factory=list)
    stopped: list[int] = field(default_factory=list)

    def list_torrents(self) -> list[Torrent]:
        return self.torrents

    def stop(self, torrent_id: int) -> None:
        self.stopped.append(torrent_id)
        for t in self.torrents:
            if t.id == torrent_id:
                t.status = 0  # mirrors Transmission's real stopped status (spike 4)

    def remove(self, torrent_id: int, delete_data: bool) -> None:
        self.removed.append((torrent_id, delete_data))
        self.torrents = [t for t in self.torrents if t.id != torrent_id]
