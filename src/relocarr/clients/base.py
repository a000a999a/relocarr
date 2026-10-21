from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class Torrent:
    id: int
    name: str
    hash: str
    percent_done: float
    status: int
    download_dir: str

    @property
    def is_complete(self) -> bool:
        # NOT isFinished -- that flag tracks "hit a seed ratio/idle limit",
        # not "download complete". Confirmed via spike 4, 2026-10-21: torrents
        # showed isFinished=false at percentDone=1.0 and status=stopped.
        return self.percent_done >= 1.0


class DownloadClient(Protocol):
    def list_torrents(self) -> list[Torrent]: ...

    def stop(self, torrent_id: int) -> None: ...

    def remove(self, torrent_id: int, delete_data: bool) -> None: ...
