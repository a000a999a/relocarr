import logging
import os
import secrets
import shutil
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Form, Header, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import VERSION, arr
from .. import library
from ..arr.payloads import parse_radarr_webhook, parse_sonarr_webhook
from ..arr.preflight import evaluate_preflight
from ..config import settings
from ..db import session
from ..disk_health import cache as disk_space_cache
from ..library import cache as movie_library_cache
from ..library import parse_title_year
from ..transfer.layout import compute_target_path
from ..transfer.safety import PathSafetyError, assert_distinct_roots

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="src/relocarr/web/templates")

_PARSERS = {"radarr": parse_radarr_webhook, "sonarr": parse_sonarr_webhook}

START_TIME = time.monotonic()

# If a scan hasn't succeeded in this long, something's actually wrong --
# a wedged mount (library.py's SCAN_TIMEOUT_SECONDS already protects a
# single attempt from hanging forever, but the background thread it gave up
# waiting on can still be stuck) rather than just "hasn't had its 10-minute
# turn yet."
STALE_SCAN_THRESHOLD_SECONDS = library.SCAN_INTERVAL_SECONDS * 3

RESTART_INSTRUCTIONS = (
    "Restart relocarr to clear it: <code>docker restart relocarr</code> from the host "
    "(or <code>./install.sh</code> from the project folder, which also rebuilds). "
    "Python cannot force-kill a hung filesystem call directly -- only a full "
    "process restart reliably does."
)


def _library_scan_status() -> dict:
    """Health of the background movie-library scan (library.py), for both
    the System page and the sidebar badge. Three states: never scanned yet
    (every scan loop takes a few seconds to start, this is expected right
    after startup); stale (last success is old enough to suggest it is
    stuck, not just waiting its turn); ok.
    """
    scanned_at = movie_library_cache.scanned_at
    if scanned_at is None:
        return {"level": "warning", "message": "Movie library has not been scanned yet."}

    age_seconds = time.time() - scanned_at
    if age_seconds > STALE_SCAN_THRESHOLD_SECONDS:
        age_minutes = int(age_seconds // 60)
        return {
            "level": "error",
            "message": f"Movie library scan has not succeeded in {age_minutes} min -- "
            f"may be stuck on an unreachable mount. {RESTART_INSTRUCTIONS}",
        }

    if movie_library_cache.last_timeout_at and movie_library_cache.last_timeout_at > scanned_at:
        return {
            "level": "warning",
            "message": "The last scan attempt timed out; showing an earlier successful scan. "
            f"If this keeps happening: {RESTART_INSTRUCTIONS}",
        }

    return {"level": "ok", "message": None}


def _sidebar_issues() -> dict:
    """Cheap, no-live-network-call issue summary for the sidebar's System
    badge -- evaluated on every page load (it's a Jinja global, called from
    base.html), so deliberately limited to fast signals: one DB count plus
    the already-scheduled library scan's own in-memory cache. Radarr/Sonarr
    connectivity itself stays on the dedicated System page, which already
    checks it live -- too slow to repeat on every navigation.
    """
    with session(settings.db_path) as conn:
        needs_attention = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE state = 'NEEDS_ATTENTION'"
        ).fetchone()["n"]

    scan_status = _library_scan_status()
    unreachable_count = len(movie_library_cache.unreachable_roots)
    disk_unreachable_count = sum(1 for e in disk_space_cache.entries.values() if not e["ok"])

    count = (
        needs_attention
        + (1 if scan_status["level"] != "ok" else 0)
        + unreachable_count
        + disk_unreachable_count
    )
    if needs_attention or scan_status["level"] == "error" or disk_unreachable_count:
        level = "error"
    elif count:
        level = "warning"
    else:
        level = "ok"
    return {"count": count, "level": level}


templates.env.globals["sidebar_issues"] = _sidebar_issues


def _check_target_folder(staging_root: str, target_root: str) -> str | None:
    """Live check for the App Detail page (Section 9: "exists, writable,
    free space, mounted"). Returns an error string, or None if all clear.
    """
    if not target_root:
        return None
    path = Path(target_root)
    if not path.is_dir():
        return "does not exist or is not a directory"
    if not os.access(path, os.W_OK):
        return "not writable"
    try:
        assert_distinct_roots(staging_root, target_root)
    except PathSafetyError as exc:
        return str(exc)
    try:
        free = shutil.disk_usage(path).free
    except OSError as exc:
        return f"could not check free space: {exc}"
    if free < 100 * 1024 * 1024:
        return f"only {free // (1024 * 1024)} MB free"
    return None


# ----------------------------------------------------------------------
# Default landing page
# ----------------------------------------------------------------------


@router.get("/")
def root():
    return RedirectResponse("/log", status_code=307)


# ----------------------------------------------------------------------
# Settings > Add App, App Config (was the top-level Dashboard/Add App)
# ----------------------------------------------------------------------


@router.get("/settings")
def settings_root():
    return RedirectResponse("/settings/apps", status_code=307)


@router.get("/settings/apps")
def app_config_list(request: Request):
    with session(settings.db_path) as conn:
        apps = conn.execute("SELECT * FROM apps ORDER BY name").fetchall()
    return templates.TemplateResponse(request, "settings_apps.html", {"apps": apps})


@router.get("/settings/apps/new")
def apps_new_form(request: Request):
    return templates.TemplateResponse(request, "apps_new.html", {"error": None, "values": {}})


@router.post("/settings/apps/new")
def apps_new_submit(
    request: Request,
    kind: str = Form(...),
    name: str = Form(...),
    base_url: str = Form(...),
    api_key: str = Form(...),
):
    values = {"kind": kind, "name": name, "base_url": base_url, "api_key": api_key}
    client = arr.make_app(kind, base_url, api_key)
    result = client.test_connection()
    if not result.ok:
        return templates.TemplateResponse(
            request,
            "apps_new.html",
            {"error": f"Connection failed: {result.error}", "values": values},
        )

    root_folders = client.list_root_folders()
    webhook_token = secrets.token_urlsafe(32)
    with session(settings.db_path) as conn:
        new_id = conn.execute(
            """INSERT INTO apps (kind, name, base_url, api_key, webhook_token, staging_root, target_root)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                kind,
                name,
                base_url,
                api_key,
                webhook_token,
                root_folders[0].path if root_folders else "",
                "",
            ),
        ).lastrowid
    return RedirectResponse(f"/settings/apps/{new_id}", status_code=303)


@router.get("/settings/apps/{app_id}")
def app_detail(request: Request, app_id: int):
    with session(settings.db_path) as conn:
        app = conn.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
        if app is None:
            raise HTTPException(404, "unknown app")
        download_clients = conn.execute(
            "SELECT * FROM download_clients WHERE app_id = ?", (app_id,)
        ).fetchall()

    target_error = _check_target_folder(app["staging_root"], app["target_root"])
    connection_result = arr.make_app(app["kind"], app["base_url"], app["api_key"]).test_connection()
    return templates.TemplateResponse(
        request,
        "app_detail.html",
        {
            "app": app,
            "download_clients": download_clients,
            "target_error": target_error,
            "connection_result": connection_result,
            "connection_error": None,
        },
    )


@router.post("/settings/apps/{app_id}/connection")
def app_connection_submit(
    request: Request,
    app_id: int,
    base_url: str = Form(...),
    api_key: str = Form(...),
):
    """Edit the *arr connection itself (URL/API key) -- the setup wizard
    only sets these once at creation; this is the only way to change them
    afterward (e.g. a rotated API key, or the *arr moved host/port).
    Re-tests the connection before saving, same as the setup wizard.
    """
    with session(settings.db_path) as conn:
        app = conn.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
        if app is None:
            raise HTTPException(404, "unknown app")
        download_clients = conn.execute(
            "SELECT * FROM download_clients WHERE app_id = ?", (app_id,)
        ).fetchall()

    client = arr.make_app(app["kind"], base_url, api_key)
    result = client.test_connection()
    if not result.ok:
        target_error = _check_target_folder(app["staging_root"], app["target_root"])
        return templates.TemplateResponse(
            request,
            "app_detail.html",
            {
                "app": app,
                "download_clients": download_clients,
                "target_error": target_error,
                "connection_result": None,
                "connection_error": f"Connection failed: {result.error}. Not saved -- the previous URL/key are still in effect.",
            },
        )

    with session(settings.db_path) as conn:
        conn.execute(
            "UPDATE apps SET base_url = ?, api_key = ? WHERE id = ?", (base_url, api_key, app_id)
        )
    return RedirectResponse(f"/settings/apps/{app_id}", status_code=303)


@router.post("/settings/apps/{app_id}")
def app_detail_submit(
    request: Request,
    app_id: int,
    target_root: str = Form(""),
    layout_mode: str = Form("flat"),
    max_file_size_gb: str = Form(""),
    conflict_policy: str = Form("skip"),
):
    max_file_size_bytes = None
    if max_file_size_gb.strip():
        try:
            max_file_size_bytes = int(float(max_file_size_gb) * 1024**3)
        except ValueError:
            pass

    with session(settings.db_path) as conn:
        conn.execute(
            """UPDATE apps SET target_root = ?, layout_mode = ?, max_file_size_bytes = ?, conflict_policy = ?
               WHERE id = ?""",
            (target_root, layout_mode, max_file_size_bytes, conflict_policy, app_id),
        )
    return RedirectResponse(f"/settings/apps/{app_id}", status_code=303)


@router.get("/settings/apps/{app_id}/preflight")
def app_preflight(request: Request, app_id: int):
    """Section 10's preflight page: live checklist of the *arr's settings."""
    with session(settings.db_path) as conn:
        app = conn.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
        if app is None:
            raise HTTPException(404, "unknown app")
        sidecar_row = conn.execute(
            "SELECT value FROM settings WHERE key = 'sidecar_extensions'"
        ).fetchone()
        sidecar_extensions = [
            e.strip() for e in (sidecar_row["value"] if sidecar_row else "srt").split(",") if e.strip()
        ]

    client = arr.make_app(app["kind"], app["base_url"], app["api_key"])
    try:
        data = client.get_preflight_data()
    except Exception as exc:  # noqa: BLE001 -- surface as a page-level error, not a 500
        return templates.TemplateResponse(
            request, "preflight.html", {"app": app, "checks": None, "fetch_error": str(exc)}
        )
    finally:
        client.close()

    checks = evaluate_preflight(
        data, staging_root=app["staging_root"], sidecar_extensions=sidecar_extensions
    )
    return templates.TemplateResponse(
        request, "preflight.html", {"app": app, "checks": checks, "fetch_error": None}
    )


# ----------------------------------------------------------------------
# Settings > Download Clients -- aggregated, Radarr-style overview across
# every app's client(s). Reuses the same add/delete routes above (clients
# are scoped to an app in the data model, same as the *arr itself scopes
# a download client to one instance) -- this page just also lets you add
# one without already being on that app's own detail page, by picking the
# app from a dropdown first.
# ----------------------------------------------------------------------


@router.get("/settings/download-clients")
def download_clients_list(request: Request):
    with session(settings.db_path) as conn:
        clients = conn.execute(
            """SELECT download_clients.*, apps.name AS app_name FROM download_clients
               JOIN apps ON apps.id = download_clients.app_id
               ORDER BY apps.name"""
        ).fetchall()
        apps = conn.execute("SELECT id, name FROM apps ORDER BY name").fetchall()
    return templates.TemplateResponse(
        request, "settings_download_clients.html", {"clients": clients, "apps": apps}
    )


@router.post("/settings/download-clients/new")
def download_clients_new(
    request: Request,
    app_id: int = Form(...),
    host: str = Form(...),
    port: int = Form(...),
    username: str = Form(""),
    password: str = Form(""),
    stop_on_completion: str | None = Form(None),
    remove_after_copy: str | None = Form(None),
):
    with session(settings.db_path) as conn:
        conn.execute(
            """INSERT INTO download_clients
               (app_id, kind, host, port, username, password, stop_on_completion, remove_after_copy)
               VALUES (?, 'transmission', ?, ?, ?, ?, ?, ?)""",
            (
                app_id,
                host,
                port,
                username or None,
                password or None,
                1 if stop_on_completion else 0,
                1 if remove_after_copy else 0,
            ),
        )
    return RedirectResponse("/settings/download-clients", status_code=303)


@router.post("/settings/download-clients/{dc_id}/delete")
def download_clients_delete(dc_id: int):
    with session(settings.db_path) as conn:
        conn.execute("DELETE FROM download_clients WHERE id = ?", (dc_id,))
    return RedirectResponse("/settings/download-clients", status_code=303)


# ----------------------------------------------------------------------
# Settings > General (was the top-level /settings form)
# ----------------------------------------------------------------------


@router.get("/settings/general")
def settings_general_form(request: Request):
    with session(settings.db_path) as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return templates.TemplateResponse(
        request, "settings_general.html", {"settings": {r["key"]: r["value"] for r in rows}}
    )


@router.post("/settings/general")
def settings_general_submit(
    request: Request,
    dry_run: str | None = Form(None),
    poll_interval_seconds: str = Form(...),
    sidecar_extensions: str = Form(...),
    verify_mode: str = Form(...),
):
    values = {
        "dry_run": "true" if dry_run else "false",
        "poll_interval_seconds": poll_interval_seconds,
        "sidecar_extensions": sidecar_extensions,
        "verify_mode": verify_mode,
    }
    with session(settings.db_path) as conn:
        for key, value in values.items():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
    return RedirectResponse("/settings/general", status_code=303)


# ----------------------------------------------------------------------
# Webhook (external-facing -- the *arr calls this. URL unchanged, never
# move this: it's already configured as a live Webhook connection URL in
# production *arr instances)
# ----------------------------------------------------------------------


@router.post("/webhook/{app_id}")
def webhook(
    app_id: int,
    payload: dict,
    x_relocarr_token: str | None = Header(None),
):
    """Receives the *arr's "Download" webhook event. Section 10: the *arr's
    Webhook connection must send this app's token in the X-Relocarr-Token
    header. Creates one job per imported file at state IMPORTED, with its
    target path already resolved (Section 5).
    """
    with session(settings.db_path) as conn:
        app = conn.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
        if app is None:
            raise HTTPException(404, "unknown app")
        if not secrets.compare_digest(x_relocarr_token or "", app["webhook_token"]):
            raise HTTPException(401, "invalid webhook token")

        if not app["target_root"]:
            raise HTTPException(409, "app has no target folder configured yet")

        parser = _PARSERS[app["kind"]]
        files = parser(payload)

        created = 0
        for f in files:
            try:
                target_path = compute_target_path(
                    source_path=f.source_path,
                    staging_root=app["staging_root"],
                    target_root=app["target_root"],
                    mode=app["layout_mode"],
                )
            except PathSafetyError:
                continue  # refuse to create a job that would write outside the target root

            max_size = app["max_file_size_bytes"]
            if max_size and f.size_bytes > max_size:
                # Section 9/16: configurable per-destination guard, added after
                # the 13.6 GB Shield-wedge incident -- flagged, never attempted.
                initial_state = "NEEDS_ATTENTION"
                error = f"file size {f.size_bytes} exceeds configured max {max_size}"
            else:
                initial_state = "IMPORTED"
                error = None

            cursor = conn.execute(
                """INSERT OR IGNORE INTO jobs
                   (app_id, download_id, media_file_id, unmonitor_id, source_path, target_path, size_bytes, state, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    app_id,
                    f.download_id,
                    f.file_id,
                    f.unmonitor_id,
                    f.source_path,
                    str(target_path),
                    f.size_bytes,
                    initial_state,
                    error,
                ),
            )
            created += cursor.rowcount

    return {"jobs_created": created}


# ----------------------------------------------------------------------
# Jobs, Log (top-level -- operational views, not configuration)
# ----------------------------------------------------------------------


@router.get("/jobs")
def jobs_list(request: Request):
    with session(settings.db_path) as conn:
        jobs = conn.execute(
            """SELECT jobs.*, apps.name AS app_name FROM jobs
               JOIN apps ON apps.id = jobs.app_id
               ORDER BY jobs.updated_at DESC LIMIT 200"""
        ).fetchall()
    return templates.TemplateResponse(request, "jobs.html", {"jobs": jobs})


SIZE_BUCKET_THRESHOLDS_GB = (2.5, 3, 4)


def _size_bucket_bounds() -> list[tuple[float, float | None]]:
    """Non-overlapping (lower, upper] bands between consecutive thresholds,
    e.g. (2.5, 3), (3, 4), (4, None) -- so each file falls into exactly one
    bucket instead of every bucket above it also counting it.
    """
    bounds = []
    for i, lower in enumerate(SIZE_BUCKET_THRESHOLDS_GB):
        upper = SIZE_BUCKET_THRESHOLDS_GB[i + 1] if i + 1 < len(SIZE_BUCKET_THRESHOLDS_GB) else None
        bounds.append((lower, upper))
    return bounds


def _classify_redownload(
    file: dict, by_path: dict, by_title_year: dict
) -> tuple[str, object | None]:
    """Which of the three redownload actions applies to a scanned file:
    - "replace": Radarr still has this exact file tracked -- delete + search.
    - "remonitor": Radarr knows the movie (by title/year) but has lost the
      file -- typically relocarr's own earlier unmonitor + delete_file once
      that transfer completed. Re-monitor + search, nothing to delete.
    - "add": no Radarr record at all -- most of a library that predates it.
      Needs Radarr's lookup API and a quality profile the caller picks.
    - "unknown": filename doesn't match "Title (Year).ext", so there's no
      reliable term to search Radarr with.
    Returns (action, match-or-None); match is only present for the first two.
    """
    match = by_path.get(file.get("path"))
    if match is not None:
        return "replace", match
    parsed = parse_title_year(file.get("name", ""))
    if parsed is not None:
        match = by_title_year.get((parsed[0].lower(), parsed[1]))
        if match is not None:
            return "remonitor", match
        return "add", None
    return "unknown", None


def _enrich_with_radarr_matches(apps, files: list[dict]) -> None:
    """Mutates each file dict in place, adding `redownload_action` (one of
    "replace"/"remonitor"/"add"/"unknown", see _classify_redownload),
    `movie_match` (the Radarr MovieMatch, for "replace"/"remonitor"), and
    `quality_profiles` (that app's list, for the Redownload form's picker).
    One GET /api/v3/movie + one GET /api/v3/qualityprofile per distinct app
    involved, not one per file.
    """
    apps_by_id = {a["id"]: a for a in apps}
    indexes_by_app: dict[int, tuple[dict, dict]] = {}
    profiles_by_app: dict[int, list] = {}
    for f in files:
        app_id = f.get("app_id")
        if app_id not in indexes_by_app:
            app_row = apps_by_id.get(app_id)
            if app_row is None or app_row["kind"] != "radarr":
                indexes_by_app[app_id] = ({}, {})
                profiles_by_app[app_id] = []
                continue
            client = arr.RadarrApp(app_row["base_url"], app_row["api_key"])
            try:
                indexes_by_app[app_id] = client.build_movie_index()
                profiles_by_app[app_id] = client.list_quality_profiles()
            except httpx.HTTPError:
                logger.exception("could not reach Radarr app %r to match library files", app_id)
                indexes_by_app[app_id] = ({}, {})
                profiles_by_app[app_id] = []
            finally:
                client.close()
        by_path, by_title_year = indexes_by_app[app_id]
        action, match = _classify_redownload(f, by_path, by_title_year)
        f["redownload_action"] = action
        f["movie_match"] = match
        f["quality_profiles"] = profiles_by_app[app_id]


@router.get("/log")
def transfer_log(
    request: Request,
    min_gb: float | None = None,
    max_gb: float | None = None,
    redownload_error: str | None = None,
    redownload_result: str | None = None,
):
    """Historical record of finished transfer attempts -- success
    (DONE), failed (NEEDS_ATTENTION), or skipped (SKIPPED) -- each with the
    timestamp it reached that state. In-flight jobs (IMPORTED through
    RECONCILED) don't appear here; see /jobs for those.

    Also shows a free-space capacity projection, and a size breakdown of
    the movie files actually sitting in each Radarr target folder right
    now -- read from `library.cache`, a periodically-refreshed scan of the
    filesystem, not from job history (which only has entries for files
    relocarr itself moved, a small fraction of a library that likely
    existed before relocarr did). Each bucket is a non-overlapping
    (min_gb, max_gb] band; ?min_gb=&max_gb= filters the file list to one,
    and each file in that list can be sent back to Radarr for a fresh,
    smaller-release search (POST /log/redownload[-bulk]).
    """
    with session(settings.db_path) as conn:
        apps = conn.execute("SELECT * FROM apps").fetchall()
        entries = conn.execute(
            """SELECT jobs.*, apps.name AS app_name FROM jobs
               JOIN apps ON apps.id = jobs.app_id
               WHERE jobs.state IN ('DONE', 'NEEDS_ATTENTION', 'SKIPPED')
               ORDER BY jobs.updated_at DESC LIMIT 500"""
        ).fetchall()

    movie_files = movie_library_cache.files
    size_buckets = [
        {
            "gb": lower,
            "upper_gb": upper,
            "count": sum(
                1
                for f in movie_files
                if f["size_bytes"] > lower * 1024**3
                and (upper is None or f["size_bytes"] <= upper * 1024**3)
            ),
        }
        for lower, upper in _size_bucket_bounds()
    ]

    filtered_movie_files = None
    if min_gb is not None:
        min_bytes = min_gb * 1024**3
        max_bytes = max_gb * 1024**3 if max_gb is not None else None
        filtered_movie_files = sorted(
            (
                f
                for f in movie_files
                if f["size_bytes"] > min_bytes and (max_bytes is None or f["size_bytes"] <= max_bytes)
            ),
            key=lambda f: f["size_bytes"],
            reverse=True,
        )
        if filtered_movie_files:
            _enrich_with_radarr_matches(apps, filtered_movie_files)

    capacity_projection = []
    seen_target_roots: set[str] = set()
    for app in apps:
        target_root = app["target_root"]
        if app["kind"] != "radarr" or not target_root or target_root in seen_target_roots:
            continue
        seen_target_roots.add(target_root)
        # Read-only lookup against disk_health's cache, never a live
        # shutil.disk_usage() call here -- confirmed live, 2026-10-24, that
        # a wedged network mount can hang that call indefinitely, which
        # used to take this whole page down with it.
        entry = disk_space_cache.entries.get(target_root)
        if entry is None or not entry["ok"]:
            continue
        free = entry["free"]
        capacity_projection.append(
            {
                "label": app["name"],
                "free_gb": free / 1024**3,
                "at_1gb": int(free // 1024**3),
                "at_2gb": int(free // (2 * 1024**3)),
            }
        )

    return templates.TemplateResponse(
        request,
        "log.html",
        {
            "entries": entries,
            "capacity_projection": capacity_projection,
            "size_buckets": size_buckets,
            "min_gb": min_gb,
            "max_gb": max_gb,
            "filtered_movie_files": filtered_movie_files,
            "redownload_error": redownload_error,
            "redownload_result": redownload_result,
            "library_scanned_at": (
                datetime.fromtimestamp(movie_library_cache.scanned_at).strftime("%Y-%m-%d %H:%M:%S")
                if movie_library_cache.scanned_at
                else None
            ),
        },
    )


def _apply_redownload(
    client: arr.RadarrApp,
    root_folder_path: str,
    file: dict,
    by_path: dict,
    by_title_year: dict,
    quality_profile_id: int | None,
) -> str:
    """Classifies one file (see _classify_redownload) and performs the
    matching Radarr action, returning a short human-readable outcome.
    Raises ValueError -- caught by the callers, never lets one bad file
    abort a bulk batch -- when there's nothing sensible to do: the
    filename doesn't parse to a title/year, it's a brand-new addition and
    no quality profile was given (there's no existing one to fall back
    to), or no release smaller than the current file can be found (see
    RadarrApp.find_smaller_release) -- the whole point of this feature is
    to shrink the library, so finding nothing better is a reason to do
    nothing, not to grab whatever Radarr would've auto-picked.
    """
    action, match = _classify_redownload(file, by_path, by_title_year)
    name = file.get("name") or file.get("path", "")
    current_size = file["size_bytes"]
    current_gb = current_size / 1024**3

    if action == "replace":
        if quality_profile_id is not None:
            client.set_quality_profile(match.movie_id, quality_profile_id)
        release = client.find_smaller_release(match.movie_id, current_size)
        if release is None:
            raise ValueError(
                f'"{name}": no release smaller than the current {current_gb:.2f} GB found -- nothing downloaded'
            )
        client.delete_file(str(match.movie_file_id))
        client.grab_release(release)
        return (
            f"redownloading {match.title or name} "
            f"({release.size_bytes / 1024**3:.2f} GB, was {current_gb:.2f} GB)"
        )

    if action == "remonitor":
        if quality_profile_id is not None:
            client.set_quality_profile(match.movie_id, quality_profile_id)
        client.set_monitored(match.movie_id)
        release = client.find_smaller_release(match.movie_id, current_size)
        if release is None:
            raise ValueError(
                f'"{name}": no release smaller than the current {current_gb:.2f} GB found -- nothing downloaded'
            )
        client.grab_release(release)
        return (
            f"redownloading {match.title or name} "
            f"({release.size_bytes / 1024**3:.2f} GB, was {current_gb:.2f} GB)"
        )

    if action == "add":
        if quality_profile_id is None:
            raise ValueError(f'"{name}" needs a quality profile picked -- Radarr never tracked it before')
        title, year = parse_title_year(name)
        added = client.lookup_and_add(title, year, quality_profile_id, root_folder_path, search_for_movie=False)
        if added is None:
            raise ValueError(f'"{name}": no Radarr lookup match for "{title} ({year})"')
        release = client.find_smaller_release(added.movie_id, current_size)
        if release is None:
            raise ValueError(
                f'"{name}": added to Radarr, but no release smaller than the current '
                f"{current_gb:.2f} GB found yet -- nothing downloaded"
            )
        client.grab_release(release)
        return (
            f"added and redownloading {added.title} "
            f"({release.size_bytes / 1024**3:.2f} GB, was {current_gb:.2f} GB)"
        )

    raise ValueError(f'"{name}": cannot tell its title/year from the filename')


def _log_redirect_url(
    min_gb: float | None, max_gb: float | None, *, error: str | None = None, result: str | None = None
) -> str:
    params = []
    if min_gb is not None:
        params.append(f"min_gb={min_gb}")
    if max_gb is not None:
        params.append(f"max_gb={max_gb}")
    if error:
        params.append(f"redownload_error={quote(error)}")
    if result:
        params.append(f"redownload_result={quote(result)}")
    return "/log" + ("?" + "&".join(params) if params else "")


@router.post("/log/redownload")
def log_redownload(
    app_id: int = Form(...),
    path: str = Form(...),
    name: str = Form(...),
    size_bytes: int = Form(...),
    quality_profile_id: str = Form(""),
    min_gb: float | None = Form(None),
    max_gb: float | None = Form(None),
):
    """The Log page's per-file Redownload button. Depending on what Radarr
    currently knows about this file (see _classify_redownload): deletes a
    still-tracked file and searches, re-monitors a movie whose file Radarr
    already lost track of (relocarr's own earlier cleanup, typically), or
    looks the title up and adds it fresh -- same three cases as the bulk
    version below. Every case only ever grabs a release smaller than the
    current file (see RadarrApp.find_smaller_release) -- if nothing
    smaller is available, nothing is downloaded, by design. An explicit,
    one-off user click -- unlike the automated import pipeline, this is
    NOT gated by the dry_run setting, the same way /jobs/{id}/retry and
    /skip aren't.
    """
    with session(settings.db_path) as conn:
        app = conn.execute("SELECT * FROM apps WHERE id = ? AND kind = 'radarr'", (app_id,)).fetchone()
    if app is None:
        raise HTTPException(status_code=404, detail="app not found")

    profile_id = int(quality_profile_id) if quality_profile_id else None
    client = arr.RadarrApp(app["base_url"], app["api_key"])
    try:
        by_path, by_title_year = client.build_movie_index()
        outcome = _apply_redownload(
            client,
            app["staging_root"],
            {"path": path, "name": name, "size_bytes": size_bytes},
            by_path,
            by_title_year,
            profile_id,
        )
    except ValueError as exc:
        return RedirectResponse(_log_redirect_url(min_gb, max_gb, error=str(exc)), status_code=303)
    except httpx.HTTPError:
        logger.exception("redownload failed for app %r, path %r", app_id, path)
        return RedirectResponse(
            _log_redirect_url(min_gb, max_gb, error="Could not reach Radarr -- see the server log."),
            status_code=303,
        )
    finally:
        client.close()

    return RedirectResponse(
        _log_redirect_url(min_gb, max_gb, result=f"Redownload: {outcome}."), status_code=303
    )


@router.post("/log/redownload-bulk")
def log_redownload_bulk(
    paths: list[str] = Form(default=[]),
    quality_profile_id: str = Form(""),
    min_gb: float | None = Form(None),
    max_gb: float | None = Form(None),
):
    """The Log page's checkbox-driven bulk redownload. Each `paths` value
    is "<app_id>|<path>" -- every row's checkbox is unchecked by default
    (unparseable-filename rows have no checkbox at all), so this only ever
    acts on files deliberately checked; a submit with nothing checked (an
    unchecked form posts no value for that field at all) does nothing, not
    "redownload everything." Grouped by app so each app's Radarr movie
    index is only built once no matter how many of its files were
    selected. A file that can't be resolved (see _apply_redownload) is
    skipped, logged, and counted -- never allowed to abort the rest of
    the batch.
    """
    if not paths:
        return RedirectResponse(
            _log_redirect_url(min_gb, max_gb, error="No files were checked -- nothing to redownload."),
            status_code=303,
        )
    files_by_path = {(f["app_id"], f["path"]): f for f in movie_library_cache.files}
    targets_by_app: dict[int, list[dict]] = {}
    for raw in paths:
        app_id_str, _, path = raw.partition("|")
        try:
            app_id = int(app_id_str)
        except ValueError:
            continue
        f = files_by_path.get((app_id, path))
        if f is not None:
            targets_by_app.setdefault(app_id, []).append(f)

    with session(settings.db_path) as conn:
        apps_by_id = {
            a["id"]: a for a in conn.execute("SELECT * FROM apps WHERE kind = 'radarr'").fetchall()
        }

    profile_id = int(quality_profile_id) if quality_profile_id else None
    succeeded = 0
    skipped = 0
    had_connection_error = False
    for app_id, files in targets_by_app.items():
        app = apps_by_id.get(app_id)
        if app is None:
            skipped += len(files)
            continue
        client = arr.RadarrApp(app["base_url"], app["api_key"])
        try:
            by_path, by_title_year = client.build_movie_index()
            for f in files:
                try:
                    outcome = _apply_redownload(
                        client, app["staging_root"], f, by_path, by_title_year, profile_id
                    )
                    logger.info("bulk redownload: %s", outcome)
                    succeeded += 1
                except ValueError as exc:
                    logger.warning("bulk redownload skipped: %s", exc)
                    skipped += 1
        except httpx.HTTPError:
            logger.exception("bulk redownload failed for app %r", app_id)
            had_connection_error = True
            skipped += len(files)
        finally:
            client.close()

    result = f"Redownload triggered for {succeeded} file{'s' if succeeded != 1 else ''}."
    if skipped:
        result += f" {skipped} skipped"
        result += " (Radarr unreachable for some) -- see the server log." if had_connection_error else "."
    return RedirectResponse(_log_redirect_url(min_gb, max_gb, result=result), status_code=303)


@router.post("/jobs/{job_id}/retry")
def job_retry(job_id: int):
    """Section 9: Jobs page Retry button. Only meaningful for a job stuck at
    NEEDS_ATTENTION -- resets it to IMPORTED so the next scheduler tick
    (jobs/worker.py) picks it back up and tries the copy again.
    """
    with session(settings.db_path) as conn:
        conn.execute(
            "UPDATE jobs SET state = 'IMPORTED', error = NULL WHERE id = ? AND state = 'NEEDS_ATTENTION'",
            (job_id,),
        )
    return RedirectResponse("/jobs", status_code=303)


@router.post("/jobs/{job_id}/skip")
def job_skip(job_id: int):
    """Section 9: Jobs page Skip button. Marks a stuck job terminal without
    retrying -- it's excluded from the open-jobs count but left exactly as
    it is on disk (no cleanup attempted for a job that never got verified).
    """
    with session(settings.db_path) as conn:
        conn.execute(
            "UPDATE jobs SET state = 'SKIPPED' WHERE id = ? AND state = 'NEEDS_ATTENTION'",
            (job_id,),
        )
    return RedirectResponse("/jobs", status_code=303)


@router.post("/jobs/{job_id}/abort")
def job_abort(job_id: int):
    """Jobs page Abort & Delete button. For a job stuck before anything on
    the *arr side has been touched yet (IMPORTED, COPYING, or
    NEEDS_ATTENTION) -- deliberately not offered once PUBLISHED or later,
    since the removal gate (jobs/worker.py) needs the job row to still
    exist to correlate sibling jobs for torrent removal.

    Best-effort deletes whatever rsync may have left at the destination:
    rsync's --partial (transfer/copier.py) renames an interrupted
    transfer's temp file to the real target filename rather than deleting
    it, so a job stuck retrying can leave a corrupt, full-filename file
    sitting at the destination -- left behind otherwise, looking like a
    real (if oddly-sized) file to anything else reading that folder.

    Safety check added after almost shipping a real bug (caught live,
    2026-10-24, before it ran against production): a *different*, already-
    completed job can target the exact same path -- e.g. a duplicate/retry
    webhook for a movie that already copied successfully under a different
    job id. Deleting "whatever's at target_path" unconditionally would
    delete that other job's legitimate, finished file. So this only
    deletes the destination file if no OTHER job has reached
    VERIFIED/PUBLISHED/RECONCILED/CLEANED/DONE for that same path --
    i.e. only when nothing else has a real claim on what's sitting there.

    Cannot interrupt an rsync subprocess that's actively mid-transfer right
    now -- it runs synchronously inside the scheduler's own background
    thread (jobs/machine.py's advance_copy -> transfer/copier.py's
    subprocess.run), with no handle exposed here to signal it. This only
    ever prevents the *next* retry and cleans up after the fact.
    """
    with session(settings.db_path) as conn:
        job = conn.execute(
            "SELECT * FROM jobs WHERE id = ? AND state IN ('IMPORTED', 'COPYING', 'NEEDS_ATTENTION')",
            (job_id,),
        ).fetchone()
        if job is None:
            return RedirectResponse("/jobs", status_code=303)

        other_claim = conn.execute(
            """SELECT 1 FROM jobs WHERE target_path = ? AND id != ?
               AND state IN ('VERIFIED', 'PUBLISHED', 'RECONCILED', 'CLEANED', 'DONE')""",
            (job["target_path"], job_id),
        ).fetchone()
        if other_claim is None:
            target = Path(job["target_path"])
            try:
                if target.exists():
                    target.unlink()
            except OSError:
                logger.exception(
                    "could not delete destination file for aborted job %r: %s", job_id, target
                )

        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    return RedirectResponse("/jobs", status_code=303)


# ----------------------------------------------------------------------
# Setup Guide -- static reference content, PLAN.md Section 10's checklist
# rendered in the UI. Most of the Radarr/Sonarr rows are also checked live
# on that app's own Preflight page; this page exists for the ones that
# aren't continuously checkable (one-time migration steps, and Transmission
# itself, which has no equivalent of the *arr's settings API to introspect).
# ----------------------------------------------------------------------


@router.get("/setup-guide")
def setup_guide(request: Request):
    return templates.TemplateResponse(request, "setup_guide.html", {})


# ----------------------------------------------------------------------
# System (modeled on Radarr's System > Status page)
# ----------------------------------------------------------------------


@router.get("/system")
def system_status(request: Request):
    with session(settings.db_path) as conn:
        apps = conn.execute("SELECT * FROM apps ORDER BY name").fetchall()
        open_jobs = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE state NOT IN ('DONE', 'SKIPPED')"
        ).fetchone()["n"]

    health = []
    if not apps:
        health.append({"message": "No apps configured yet.", "level": "warning"})
    for app in apps:
        client = arr.make_app(app["kind"], app["base_url"], app["api_key"])
        result = client.test_connection()
        client.close()
        if not result.ok:
            health.append(
                {"message": f"{app['name']}: unreachable -- {result.error}", "level": "error"}
            )

    # Read-only lookup against disk_health's cache, never a live
    # shutil.disk_usage() call here -- confirmed live, 2026-10-24, that a
    # wedged network mount can hang that call indefinitely, which used to
    # take this whole page down with it (along with /log's capacity
    # projection, the only other place that used to call it directly).
    disk_space = []
    seen_paths: set[str] = set()
    for app in apps:
        for label, path in (("staging", app["staging_root"]), ("target", app["target_root"])):
            if not path or path in seen_paths:
                continue
            seen_paths.add(path)
            entry = disk_space_cache.entries.get(path)
            if entry is None:
                disk_space.append(
                    {"path": path, "label": f"{app['name']} ({label})", "ok": None}  # pending, not yet an error
                )
            elif entry["ok"]:
                disk_space.append(
                    {
                        "path": path,
                        "label": f"{app['name']} ({label})",
                        "free": entry["free"],
                        "total": entry["total"],
                        "ok": True,
                    }
                )
            else:
                disk_space.append(
                    {"path": path, "label": f"{app['name']} ({label})", "error": entry["error"], "ok": False}
                )

    uptime_seconds = int(time.monotonic() - START_TIME)

    return templates.TemplateResponse(
        request,
        "system.html",
        {
            "health": health,
            "disk_space": disk_space,
            "version": VERSION,
            "db_path": str(settings.db_path),
            "in_docker": Path("/.dockerenv").exists(),
            "uptime_seconds": uptime_seconds,
            "app_count": len(apps),
            "open_jobs": open_jobs,
            "library_scan": _library_scan_status(),
            "library_scanned_at": (
                datetime.fromtimestamp(movie_library_cache.scanned_at).strftime("%Y-%m-%d %H:%M:%S")
                if movie_library_cache.scanned_at
                else None
            ),
            "unreachable_roots": movie_library_cache.unreachable_roots,
            "disk_checked_at": (
                datetime.fromtimestamp(disk_space_cache.checked_at).strftime("%Y-%m-%d %H:%M:%S")
                if disk_space_cache.checked_at
                else None
            ),
        },
    )
