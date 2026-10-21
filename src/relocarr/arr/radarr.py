from __future__ import annotations

import httpx

from .base import ConnectionTestResult, QueueEntry, RootFolder


class RadarrApp:
    """Radarr v3 API client. Endpoints and field names confirmed against a
    real instance (Radarr 6.4.4) during PLAN.md's spike testing, 2026-10-21.
    """

    def __init__(self, base_url: str, api_key: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={"X-Api-Key": api_key},
            timeout=timeout,
        )

    def test_connection(self) -> ConnectionTestResult:
        try:
            resp = self._client.get("/api/v3/system/status")
            resp.raise_for_status()
            return ConnectionTestResult(ok=True, version=resp.json().get("version"))
        except httpx.HTTPError as exc:
            return ConnectionTestResult(ok=False, error=str(exc))

    def list_root_folders(self) -> list[RootFolder]:
        resp = self._client.get("/api/v3/rootfolder")
        resp.raise_for_status()
        return [RootFolder(id=rf["id"], path=rf["path"]) for rf in resp.json()]

    def delete_file(self, file_id: str) -> None:
        # Confirmed live, spike 8 (2026-10-21): leaves the per-movie folder
        # behind, empty, regardless of Radarr's own folder-cleanup settings.
        # Pruning it is relocarr's job (transfer/safety.py + the CLEANED step).
        resp = self._client.delete(f"/api/v3/moviefile/{file_id}")
        if resp.status_code not in (200, 404):
            resp.raise_for_status()

    def unmonitor(self, media_id: int) -> None:
        resp = self._client.put(
            "/api/v3/movie/editor",
            json={"movieIds": [media_id], "monitored": False},
        )
        resp.raise_for_status()

    def get_queue(self) -> list[QueueEntry]:
        resp = self._client.get("/api/v3/queue", params={"includeUnknownMovieItems": "true"})
        resp.raise_for_status()
        return [
            QueueEntry(
                download_id=r["downloadId"],
                tracked_download_state=r["trackedDownloadState"],
                tracked_download_status=r["trackedDownloadStatus"],
            )
            for r in resp.json()["records"]
            if "downloadId" in r
        ]

    def close(self) -> None:
        self._client.close()
