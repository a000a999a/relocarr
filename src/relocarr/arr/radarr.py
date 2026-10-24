from __future__ import annotations

import httpx

from .base import (
    ConnectionTestResult,
    DownloadClientSettings,
    MovieMatch,
    NotificationSettings,
    PreflightData,
    QualityProfile,
    QueueEntry,
    Release,
    RootFolder,
)

# A candidate release must come in at or under this fraction of the current
# file's size to count as "worth redownloading" -- a release that's 99% of
# the current size wastes bandwidth and a download slot for essentially
# nothing. 0.95 = at least a 5% reduction.
MIN_SIZE_REDUCTION_RATIO = 0.95

# A release with at least this many seeders is considered "healthy" and
# outranks anything with fewer, regardless of size -- a torrent that won't
# actually finish is worse than a slightly larger one that will. Below this
# threshold is still eligible, just ranked lower (see find_smaller_release).
MIN_SEEDERS_PREFERRED = 10

# Radarr's interactive search (GET /api/v3/release) queries every configured
# indexer live before responding -- confirmed live, 2026-10-24, that this can
# genuinely take longer than the client's normal 10s default (a real search
# timed out and was misreported to the user as "Could not reach Radarr",
# when Radarr was never actually unreachable). Only this one call gets the
# longer budget; everything else stays fast-failing at the normal default.
RELEASE_SEARCH_TIMEOUT_SECONDS = 60.0


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

    def list_quality_profiles(self) -> list[QualityProfile]:
        resp = self._client.get("/api/v3/qualityprofile")
        resp.raise_for_status()
        return [QualityProfile(id=p["id"], name=p["name"]) for p in resp.json()]

    def build_movie_index(self) -> tuple[dict[str, MovieMatch], dict[tuple[str, int | None], MovieMatch]]:
        """One GET /api/v3/movie, indexed two ways, to resolve a filesystem
        file back to the right Radarr action without one call per file:

        - by_path: movies that currently have a file, keyed by that file's
          path -- the only identifier a filesystem scan shares with what
          Radarr reports (the whole design already requires the same path
          to be visible to both, see PLAN.md Section 2). A hit here means
          "delete this file and search" is the right move.
        - by_title_year: every movie, found-with-a-file or not, keyed by
          (lowercased title, year). A hit here with no corresponding
          by_path entry means Radarr already knows this movie but has lost
          track of its file -- exactly what relocarr's own unmonitor +
          delete_file does once a transfer completes (PLAN.md Section 5) --
          so the right move is to re-monitor and search, not delete+search.
        """
        resp = self._client.get("/api/v3/movie")
        resp.raise_for_status()
        by_path: dict[str, MovieMatch] = {}
        by_title_year: dict[tuple[str, int | None], MovieMatch] = {}
        for m in resp.json():
            movie_file = m.get("movieFile")
            match = MovieMatch(
                movie_id=m["id"],
                movie_file_id=movie_file["id"] if movie_file else None,
                quality_profile_id=m["qualityProfileId"],
                title=m.get("title", ""),
                monitored=m.get("monitored", False),
                has_file=bool(movie_file),
            )
            if movie_file:
                by_path[movie_file["path"]] = match
            by_title_year[(m.get("title", "").strip().lower(), m.get("year"))] = match
        return by_path, by_title_year

    def find_movie_by_file_path(self, path: str) -> MovieMatch | None:
        by_path, _ = self.build_movie_index()
        return by_path.get(path)

    def _update_movie(self, movie_id: int, **fields) -> None:
        movie_resp = self._client.get(f"/api/v3/movie/{movie_id}")
        movie_resp.raise_for_status()
        movie = movie_resp.json()
        movie.update(fields)
        put_resp = self._client.put(f"/api/v3/movie/{movie_id}", json=movie)
        put_resp.raise_for_status()

    def set_quality_profile(self, movie_id: int, quality_profile_id: int) -> None:
        self._update_movie(movie_id, qualityProfileId=quality_profile_id)

    def set_monitored(self, movie_id: int) -> None:
        """A movie Radarr already knows about but has lost its file for --
        typically relocarr's own earlier unmonitor + delete_file, once a
        transfer completed (PLAN.md Section 5). Re-monitoring it is what
        makes Radarr consider it missing and worth searching for again.
        """
        self._update_movie(movie_id, monitored=True)

    def find_smaller_release(self, movie_id: int, current_size_bytes: int) -> Release | None:
        """Radarr's own interactive search (the same list its manual-search
        UI shows) -- read-only, nothing is grabbed yet. Used instead of
        just triggering an automatic MoviesSearch because Radarr's own
        auto-pick has no concept of "smaller at the same quality": it only
        ever replaces a file it judges a quality *upgrade*, so left to
        itself it will either grab nothing (nothing looks like an upgrade)
        or grab something that isn't actually smaller.

        Radarr marks every candidate `rejected` once a movie already has a
        file meeting its quality profile's cutoff, *regardless of size* --
        "Existing file meets cutoff" / "...does not allow upgrades" are
        exactly the rejections this method exists to override, since our
        goal is a smaller file at the same approved quality, not a quality
        upgrade. Any OTHER rejection reason (banned group, wrong language,
        over the profile's own size cap, ...) is a real disqualifier and
        is still respected.

        Hard requirements (anything failing these is dropped entirely):
        at least MIN_SIZE_REDUCTION_RATIO smaller than `current_size_bytes`
        -- the caller must not download "sideways" -- and at least one
        seeder, since a zero-seeder release can never actually finish.

        Among what's left, ranked in this order (2026-10-24, matched to
        how the user actually picks manually from Radarr's own search UI):
        releases with >= MIN_SEEDERS_PREFERRED seeders beat everything
        with fewer, but if nothing clears that bar the best-seeded of what
        remains still wins rather than returning nothing (deliberately --
        "if there's only a seeder for larger files, I accept a larger
        file" still applies within the already-smaller-than-current set);
        then higher resolution (1080p over 720p); then more seeders; then
        smallest size as the final tiebreaker. Returns None only when
        nothing meets the two hard requirements above.
        """
        resp = self._client.get(
            "/api/v3/release", params={"movieId": movie_id}, timeout=RELEASE_SEARCH_TIMEOUT_SECONDS
        )
        resp.raise_for_status()

        candidates = []
        for r in resp.json():
            reasons = r.get("rejections") or []
            disqualifying = [
                reason
                for reason in reasons
                if "upgrade" not in reason.lower() and "cutoff" not in reason.lower()
            ]
            if disqualifying:
                continue
            if r["size"] >= current_size_bytes * MIN_SIZE_REDUCTION_RATIO:
                continue
            if r.get("seeders", 0) <= 0:
                continue
            candidates.append(r)

        if not candidates:
            return None

        def _rank(r: dict) -> tuple[bool, int, int, int]:
            seeders = r.get("seeders", 0)
            resolution = r.get("quality", {}).get("quality", {}).get("resolution", 0)
            return (seeders >= MIN_SEEDERS_PREFERRED, resolution, seeders, -r["size"])

        best = max(candidates, key=_rank)
        return Release(
            guid=best["guid"], indexer_id=best["indexerId"], size_bytes=best["size"], title=best["title"]
        )

    def grab_release(self, release: Release) -> None:
        resp = self._client.post(
            "/api/v3/release", json={"guid": release.guid, "indexerId": release.indexer_id}
        )
        resp.raise_for_status()

    def lookup_and_add(
        self,
        title: str,
        year: int,
        quality_profile_id: int,
        root_folder_path: str,
        *,
        search_for_movie: bool = True,
    ) -> MovieMatch | None:
        """A file Radarr has never tracked at all -- most of a library that
        predates it. Looks the title/year up the same way Radarr's own "Add
        New" search does, adds it, and returns the created movie, or None
        if the lookup found nothing. The caller is responsible for
        choosing a sane `quality_profile_id` -- there's no existing one to
        fall back to. `search_for_movie=False` adds it monitored without
        queuing Radarr's own blind auto-search -- used by the Redownload
        flow, which does its own size-aware search via
        find_smaller_release + grab_release right after adding instead.
        """
        lookup_resp = self._client.get("/api/v3/movie/lookup", params={"term": f"{title} {year}"})
        lookup_resp.raise_for_status()
        results = lookup_resp.json()
        candidate = next((m for m in results if m.get("year") == year), results[0] if results else None)
        if candidate is None:
            return None

        candidate["qualityProfileId"] = quality_profile_id
        candidate["rootFolderPath"] = root_folder_path
        candidate["monitored"] = True
        candidate["addOptions"] = {"searchForMovie": search_for_movie}
        add_resp = self._client.post("/api/v3/movie", json=candidate)
        add_resp.raise_for_status()
        added = add_resp.json()
        return MovieMatch(
            movie_id=added["id"],
            movie_file_id=None,
            quality_profile_id=added["qualityProfileId"],
            title=added.get("title", title),
            monitored=True,
            has_file=False,
        )

    def close(self) -> None:
        self._client.close()
