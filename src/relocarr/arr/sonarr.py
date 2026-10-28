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

# get_queue()'s own page size -- see get_queue's docstring, and
# arr/radarr.py's identical constant/fix, for why this can't be left unset.
QUEUE_PAGE_SIZE = 200


class SonarrApp:
    """Sonarr v3 API client. Same endpoint shapes as Radarr for the calls
    used here, confirmed against a real instance during spike testing,
    2026-10-21 -- except unmonitor, which targets episodes, not the series
    (PLAN.md Section 6: the series stays monitored so future episodes still
    download).
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
        resp = self._client.delete(f"/api/v3/episodefile/{file_id}")
        if resp.status_code not in (200, 404):
            resp.raise_for_status()

    def unmonitor(self, media_id: int) -> None:
        # media_id here is an *episode* id, not a series id -- the series
        # itself must stay monitored (Section 6).
        resp = self._client.put(
            "/api/v3/episode/monitor",
            json={"episodeIds": [media_id], "monitored": False},
        )
        resp.raise_for_status()

    def get_queue(self) -> list[QueueEntry]:
        """See arr/radarr.py's `RadarrApp.get_queue` docstring -- same bug,
        same fix: without `pageSize`, Sonarr also defaults to 10, which
        silently truncates any real queue larger than that on every poll.
        """
        entries: list[QueueEntry] = []
        page = 1
        fetched = 0
        total: int | None = None
        while total is None or fetched < total:
            resp = self._client.get(
                "/api/v3/queue",
                params={"includeUnknownSeriesItems": "true", "page": page, "pageSize": QUEUE_PAGE_SIZE},
            )
            resp.raise_for_status()
            data = resp.json()
            records = data["records"]
            if not records:
                break
            fetched += len(records)
            total = data.get("totalRecords", fetched)
            entries.extend(
                QueueEntry(
                    download_id=r["downloadId"],
                    tracked_download_state=r["trackedDownloadState"],
                    tracked_download_status=r["trackedDownloadStatus"],
                )
                for r in records
                if "downloadId" in r
            )
            page += 1
        return entries

    def get_preflight_data(self) -> PreflightData:
        """Section 10's Sonarr prerequisite checklist. Field names confirmed
        live during tonight's manual migration walkthrough (2026-10-21) --
        same shapes as Radarr except renameEpisodes (not renameMovies) and
        the notification trigger field names (onSeriesAdd/onSeriesDelete/
        onEpisodeFileDelete/onEpisodeFileDeleteForUpgrade, not the Movie
        equivalents).
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
            rename_enabled=naming.json()["renameEpisodes"],
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
                            "onSeriesAdd",
                            "onSeriesDelete",
                            "onEpisodeFileDelete",
                            "onEpisodeFileDeleteForUpgrade",
                        )
                    ),
                )
                for n in notif_list
            ],
            webhook_configured=any(n["implementation"] == "Webhook" for n in notif_list),
        )

    def close(self) -> None:
        self._client.close()
