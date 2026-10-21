from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ImportedFile:
    """One file from a webhook "Download" event, ready to become a job row.

    `unmonitor_id` is what gets passed to ArrApp.unmonitor(): for Radarr
    that's the movie id, for Sonarr it's an *episode* id, not the series id
    (Section 6 -- the series must stay monitored so future episodes still
    download).
    """

    unmonitor_id: int
    file_id: str
    source_path: str
    size_bytes: int
    download_id: str | None
    is_upgrade: bool


def parse_radarr_webhook(payload: dict) -> list[ImportedFile]:
    """Confirmed live against a real Radarr 6.4.4 "Download" event, spike 2
    (2026-10-21) -- see docs/webhook-samples/. `downloadId` is absent on a
    manually-imported file (not driven by the tracked download queue); such
    jobs can never pass the removal gate and should skip straight to
    cleanup once reconciled.
    """
    if payload.get("eventType") != "Download":
        return []

    movie = payload.get("movie")
    movie_file = payload.get("movieFile")
    if not movie or not movie_file:
        return []

    return [
        ImportedFile(
            unmonitor_id=movie["id"],
            file_id=str(movie_file["id"]),
            source_path=movie_file["path"],
            size_bytes=movie_file["size"],
            download_id=payload.get("downloadId"),
            is_upgrade=payload.get("isUpgrade", False),
        )
    ]


def parse_sonarr_webhook(payload: dict) -> list[ImportedFile]:
    """NOT yet confirmed against a real payload -- Sonarr has zero tracked
    series as of 2026-10-21 (PLAN.md spike 7 rest, blocked). Field names
    (`episodeFile` singular / `episodeFiles` plural, `episodes[]`) follow
    the Servarr docs' documented shape; treat as best-effort until a real
    capture exists, same caveat as PLAN.md Section 6.

    Known limitation: a multi-episode file maps to several `episodes[]`
    entries but only one `episodeFile`; this takes the *first* episode's id
    for unmonitor_id and does not yet unmonitor the others individually.
    """
    if payload.get("eventType") != "Download":
        return []

    episode_files = payload.get("episodeFiles") or (
        [payload["episodeFile"]] if payload.get("episodeFile") else []
    )
    episodes = payload.get("episodes") or []
    if not episode_files or not episodes:
        return []

    first_episode_id = episodes[0]["id"]
    return [
        ImportedFile(
            unmonitor_id=first_episode_id,
            file_id=str(ef["id"]),
            source_path=ef["path"],
            size_bytes=ef["size"],
            download_id=payload.get("downloadId"),
            is_upgrade=payload.get("isUpgrade", False),
        )
        for ef in episode_files
    ]
