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


class ArrApp(Protocol):
    """Interface implemented by arr/radarr.py and arr/sonarr.py.

    Kept deliberately small for v0.1 -- just what the setup wizard and
    preflight page need. Import/job-related calls (manual import, delete
    file, unmonitor) get added alongside the job worker.
    """

    def test_connection(self) -> ConnectionTestResult: ...

    def list_root_folders(self) -> list[RootFolder]: ...
