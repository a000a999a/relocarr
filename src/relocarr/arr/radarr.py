from __future__ import annotations

import httpx

from .base import (
    ConnectionTestResult,
    DownloadClientSettings,
    NotificationSettings,
    PreflightData,
    QueueEntry,
    RootFolder,
)


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

    def get_preflight_data(self) -> PreflightData:
        """Section 10's Radarr prerequisite checklist, read from the same
        endpoints and fields confirmed live during tonight's manual
        migration walkthrough (2026-10-21): /config/downloadclient
        (enableCompletedDownloadHandling), /downloadclient
        (removeCompletedDownloads per client), /config/mediamanagement
        (recycleBin, copyUsingHardlinks, importExtraFiles,
        extraFileExtensions), /config/naming (renameMovies), /notification
        (per-connection trigger booleans -- implementation "PlexServer" for
        the Plex case checked here).
        """
        dc_config = self._client.get("/api/v3/config/downloadclient")
        dc_config.raise_for_status()

        clients = self._client.get("/api/v3/downloadclient")
        clients.raise_for_status()

        media_mgmt = self._client.get("/api/v3/config/mediamanagement")
        media_mgmt.raise_for_status()

        naming = self._client.get("/api/v3/config/naming")
        naming.raise_for_status()

        notifications = self._client.get("/api/v3/notification")
        notifications.raise_for_status()

        mm = media_mgmt.json()
        notif_list = notifications.json()

        return PreflightData(
            root_folders=self.list_root_folders(),
            completed_download_handling_enabled=dc_config.json()["enableCompletedDownloadHandling"],
            download_clients=[
                DownloadClientSettings(
                    name=c["name"], remove_completed_downloads=c["removeCompletedDownloads"]
                )
                for c in clients.json()
            ],
            recycle_bin=mm["recycleBin"],
            use_hardlinks=mm["copyUsingHardlinks"],
            rename_enabled=naming.json()["renameMovies"],
            import_extra_files=mm["importExtraFiles"],
            extra_file_extensions=[e.strip() for e in mm["extraFileExtensions"].split(",") if e.strip()],
            notifications=[
                NotificationSettings(
                    name=n["name"],
                    implementation=n["implementation"],
                    any_trigger_enabled=any(
                        n.get(k, False)
                        for k in (
                            "onDownload",
                            "onUpgrade",
                            "onImportComplete",
                            "onRename",
                            "onMovieAdded",
                            "onMovieDelete",
                            "onMovieFileDelete",
                        )
                    ),
                )
                for n in notif_list
            ],
            webhook_configured=any(n["implementation"] == "Webhook" for n in notif_list),
        )

    def close(self) -> None:
        self._client.close()
