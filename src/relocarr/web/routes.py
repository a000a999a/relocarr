import logging
import os
import secrets
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Form, Header, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from .. import VERSION, arr
from .. import library
from .. import plex
from ..arr.payloads import parse_radarr_webhook, parse_sonarr_webhook
from ..arr.preflight import evaluate_preflight
from ..arr.radarr import MIN_SIZE_REDUCTION_RATIO
from ..clients.transmission import TransmissionClient
from ..config import settings
from ..db import session
from ..disk_health import cache as disk_space_cache
from ..disk_health import folder_usage_cache
from ..library import cache as movie_library_cache
from ..library import find_duplicate_groups
from ..library import find_duplicate_groups_from_plex
from ..library import parse_title_year
from ..plex import cache as plex_quality_cache
from ..transfer.layout import compute_target_path
from ..transfer.safety import PathSafetyError, assert_distinct_roots
from ..version_check import cache as version_check_cache
from ..version_check import is_newer

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


def _plex_status() -> dict:
    """Plex isn't a transfer destination -- it has none of the staging/
    target/layout/download-client concepts the `apps` table and its
    Radarr/Sonarr-specific routes (webhook dispatch, Preflight, every
    `arr.make_app(app["kind"], ...)` call) are built around, so it's kept
    out of that table entirely rather than shoehorned in as a third
    `kind` that half the app's queries would need to special-case around.
    Surfaced instead as a status card on the Add App and App Config
    pages, backed by the same plain settings-table storage
    /settings/plex already uses.
    """
    with session(settings.db_path) as conn:
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE key IN ('plex_base_url', 'plex_token')"
        ).fetchall()
    values = {r["key"]: r["value"] for r in rows}
    return {"configured": bool(values.get("plex_base_url") and values.get("plex_token"))}


@router.get("/settings/apps")
def app_config_list(request: Request):
    with session(settings.db_path) as conn:
        apps = conn.execute("SELECT * FROM apps ORDER BY name").fetchall()
    return templates.TemplateResponse(
        request, "settings_apps.html", {"apps": apps, "plex_status": _plex_status()}
    )


@router.get("/settings/apps/new")
def apps_new_form(request: Request):
    return templates.TemplateResponse(
        request, "apps_new.html", {"error": None, "values": {}, "plex_status": _plex_status()}
    )


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
            {"error": f"Connection failed: {result.error}", "values": values, "plex_status": _plex_status()},
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
    min_redownload_reduction_pct: str = Form(""),
):
    max_file_size_bytes = None
    if max_file_size_gb.strip():
        try:
            max_file_size_bytes = int(float(max_file_size_gb) * 1024**3)
        except ValueError:
            pass

    # Blank, non-numeric, or out of a sane 1-99 range -> NULL, which
    # find_smaller_release treats as "use the built-in default" (see
    # _min_reduction_ratio) -- never silently saves a value that would
    # mean "accept anything" (0) or "must be free" (100+).
    reduction_pct = None
    if min_redownload_reduction_pct.strip():
        try:
            parsed_pct = int(min_redownload_reduction_pct)
            if 1 <= parsed_pct <= 99:
                reduction_pct = parsed_pct
        except ValueError:
            pass

    with session(settings.db_path) as conn:
        conn.execute(
            """UPDATE apps SET target_root = ?, layout_mode = ?, max_file_size_bytes = ?,
               conflict_policy = ?, min_redownload_reduction_pct = ?
               WHERE id = ?""",
            (target_root, layout_mode, max_file_size_bytes, conflict_policy, reduction_pct, app_id),
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
    bwlimit_mbps: str = Form(...),
):
    values = {
        "dry_run": "true" if dry_run else "false",
        "poll_interval_seconds": poll_interval_seconds,
        "sidecar_extensions": sidecar_extensions,
        "verify_mode": verify_mode,
        "bwlimit_mbps": bwlimit_mbps,
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
# Settings > Plex -- optional, only used to show real resolution/codec/
# bitrate on /log/duplicates (library.py's own file scan can only ever
# see a filename and a size, not what's actually inside the file).
# Singleton: one Plex server, stored as two more rows in the generic
# `settings` table rather than a dedicated table, same as dry_run etc.
# ----------------------------------------------------------------------


@router.get("/settings/plex")
def settings_plex_form(request: Request):
    with session(settings.db_path) as conn:
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE key IN ('plex_base_url', 'plex_token')"
        ).fetchall()
    return templates.TemplateResponse(
        request,
        "settings_plex.html",
        {
            "plex_settings": {r["key"]: r["value"] for r in rows},
            "connection_result": None,
            "connection_error": None,
        },
    )


@router.post("/settings/plex")
def settings_plex_submit(
    request: Request,
    plex_base_url: str = Form(""),
    plex_token: str = Form(""),
):
    # Confirmed live, 2026-10-25: Plex's own "find your token" instructions
    # have a user copy the token out of a URL's query string, and it's an
    # easy slip to paste the whole "X-Plex-Token=<value>" fragment instead
    # of just <value> -- strip it off if present rather than silently
    # saving a token that will fail every real request.
    plex_token = plex_token.strip()
    if plex_token.lower().startswith("x-plex-token="):
        plex_token = plex_token.split("=", 1)[1].strip()

    connection_result = None
    connection_error = None
    if plex_base_url and plex_token:
        client = plex.PlexClient(plex_base_url, plex_token)
        try:
            result = client.test_connection()
        finally:
            client.close()
        if not result.ok:
            connection_error = f"Connection failed: {result.error}. Not saved -- the previous settings are still in effect."
        else:
            connection_result = result

    if connection_error:
        with session(settings.db_path) as conn:
            rows = conn.execute(
                "SELECT key, value FROM settings WHERE key IN ('plex_base_url', 'plex_token')"
            ).fetchall()
        return templates.TemplateResponse(
            request,
            "settings_plex.html",
            {
                "plex_settings": {r["key"]: r["value"] for r in rows},
                "connection_result": None,
                "connection_error": connection_error,
            },
        )

    with session(settings.db_path) as conn:
        for key, value in {"plex_base_url": plex_base_url, "plex_token": plex_token}.items():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
    # Deliberately render here instead of redirecting: a redirect to GET
    # /settings/plex throws connection_result away entirely (that handler
    # never re-tests), so the "Connected" card could never actually appear
    # after a successful Test & Save -- confirmed live, 2026-10-28.
    return templates.TemplateResponse(
        request,
        "settings_plex.html",
        {
            "plex_settings": {"plex_base_url": plex_base_url, "plex_token": plex_token},
            "connection_result": connection_result,
            "connection_error": None,
        },
    )


@router.get("/plex/poster/{rating_key}")
def plex_poster(rating_key: int):
    """Proxies a resized poster image from Plex, server-side, so the
    X-Plex-Token Plex requires for this (confirmed live -- a request
    without it 401s) never has to appear in page source/browser history,
    unlike Radarr's TMDB-hosted posters, which the browser can load
    directly. A single small image fetch, not the kind of expensive
    library-wide call disk_health/library/plex's own background loop
    exist to keep out of request handlers -- bounded by PlexClient's own
    default timeout, so a slow/dead Plex server can only ever stall this
    one <img> tag's request, never the page around it.
    """
    with session(settings.db_path) as conn:
        rows = {
            r["key"]: r["value"]
            for r in conn.execute(
                "SELECT key, value FROM settings WHERE key IN ('plex_base_url', 'plex_token')"
            ).fetchall()
        }
    base_url = rows.get("plex_base_url", "")
    token = rows.get("plex_token", "")
    if not base_url or not token:
        raise HTTPException(status_code=404, detail="Plex not configured")

    client = plex.PlexClient(base_url, token)
    try:
        content, content_type = client.fetch_poster(rating_key)
    except httpx.HTTPError:
        logger.exception("could not fetch Plex poster for ratingKey %r", rating_key)
        raise HTTPException(status_code=502, detail="Plex unreachable") from None
    finally:
        client.close()

    # Posters don't change once a movie's already in the library -- cache
    # in the browser rather than re-fetching from Plex on every page view.
    return Response(content=content, media_type=content_type, headers={"Cache-Control": "public, max-age=86400"})


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


JOB_STATES = (
    "IMPORTED",
    "COPYING",
    "VERIFIED",
    "PUBLISHED",
    "RECONCILED",
    "CLEANED",
    "DONE",
    "NEEDS_ATTENTION",
    "SKIPPED",
)


@router.get("/jobs")
def jobs_list(
    request: Request,
    app_id: str | None = None,
    state: str | None = None,
    target: str | None = None,
    error_only: bool = False,
):
    # Same fix as /log's log_app_id (2026-10-29): `int | None` 422s on the
    # filter form's "All apps" option (value=""), since FastAPI/Pydantic
    # won't coerce an empty string to an optional int. Parsed by hand.
    app_id_filter: int | None = None
    if app_id:
        try:
            app_id_filter = int(app_id)
        except ValueError:
            pass

    with session(settings.db_path) as conn:
        apps = conn.execute("SELECT * FROM apps ORDER BY name").fetchall()

        query = """SELECT jobs.*, apps.name AS app_name FROM jobs
                   JOIN apps ON apps.id = jobs.app_id WHERE 1=1"""
        params: list = []
        if app_id_filter:
            query += " AND jobs.app_id = ?"
            params.append(app_id_filter)
        if state:
            query += " AND jobs.state = ?"
            params.append(state)
        if target:
            query += " AND jobs.target_path LIKE ?"
            params.append(f"%{target}%")
        if error_only:
            query += " AND jobs.error IS NOT NULL"
        query += " ORDER BY jobs.updated_at DESC LIMIT 200"
        jobs = conn.execute(query, params).fetchall()

    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "jobs": jobs,
            "apps": apps,
            "job_states": JOB_STATES,
            "filter_app_id": app_id_filter,
            "filter_state": state,
            "filter_target": target,
            "filter_error_only": error_only,
        },
    )


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


def _enrich_with_plex_poster_links(files: list[dict]) -> None:
    """Mutates each file dict in place with `plex_quality` (pre-existing),
    `runtime_minutes` (this file's own length, from the same Plex lookup --
    None unless Plex matched this file), plus `plex_poster_url` (relocarr's
    own proxy route, never Plex directly -- see plex.PlexClient.fetch_poster
    for why) and `plex_link_url` (deep link to the movie's own page in Plex
    Web), both None unless Plex is configured AND this specific file was
    actually matched in Plex's library. Deliberately no fallback to a Radarr
    poster when Plex isn't configured -- PLAN.md's item 4 spec calls for
    a "Plex integration needed" placeholder instead; templates handle
    that themselves off of `plex_quality_cache.checked_at`/`.index`
    already being in their context (see transfer_log/log_duplicates).
    """
    base_url = plex_quality_cache.base_url
    machine_identifier = plex_quality_cache.machine_identifier
    for f in files:
        quality = plex_quality_cache.index.get(f.get("name"))
        f["plex_quality"] = quality
        duration_ms = quality.get("duration_ms") if quality else None
        f["runtime_minutes"] = round(duration_ms / 60_000) if duration_ms else None
        rating_key = quality.get("rating_key") if quality else None
        if rating_key is not None and base_url and machine_identifier:
            f["plex_poster_url"] = f"/plex/poster/{rating_key}"
            f["plex_link_url"] = plex.build_plex_web_url(base_url, machine_identifier, rating_key)
        else:
            f["plex_poster_url"] = None
            f["plex_link_url"] = None


TRANSFER_LOG_STATES = ("DONE", "NEEDS_ATTENTION", "SKIPPED")


@router.get("/log")
def transfer_log(
    request: Request,
    min_gb: float | None = None,
    max_gb: float | None = None,
    redownload_error: str | None = None,
    redownload_result: str | None = None,
    log_app_id: str | None = None,
    log_state: str | None = None,
    log_error_only: bool = False,
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
    # log_app_id is deliberately `str | None`, not `int | None` -- confirmed
    # live, 2026-10-29: FastAPI/Pydantic refuses to coerce an empty string
    # to an optional int, so the filter form's own "All apps" option
    # (value="", the unfiltered default) 422'd on every single submit. Parsed
    # by hand instead, same as every other filter value on this page.
    app_id_filter: int | None = None
    if log_app_id:
        try:
            app_id_filter = int(log_app_id)
        except ValueError:
            pass

    with session(settings.db_path) as conn:
        apps = conn.execute("SELECT * FROM apps").fetchall()

        entries_query = """SELECT jobs.*, apps.name AS app_name FROM jobs
                            JOIN apps ON apps.id = jobs.app_id
                            WHERE jobs.state IN ('DONE', 'NEEDS_ATTENTION', 'SKIPPED')"""
        entries_params: list = []
        if app_id_filter:
            entries_query += " AND jobs.app_id = ?"
            entries_params.append(app_id_filter)
        if log_state:
            entries_query += " AND jobs.state = ?"
            entries_params.append(log_state)
        if log_error_only:
            entries_query += " AND jobs.error IS NOT NULL"
        entries_query += " ORDER BY jobs.updated_at DESC LIMIT 500"
        entries = conn.execute(entries_query, entries_params).fetchall()

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
            _enrich_with_plex_poster_links(filtered_movie_files)
            for f in filtered_movie_files:
                mtime = f.get("mtime")
                f["mtime_str"] = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d") if mtime else None

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
            "apps": apps,
            "transfer_log_states": TRANSFER_LOG_STATES,
            "filter_log_app_id": app_id_filter,
            "filter_log_state": log_state,
            "filter_log_error_only": log_error_only,
            "library_scanned_at": (
                datetime.fromtimestamp(movie_library_cache.scanned_at).strftime("%Y-%m-%d %H:%M:%S")
                if movie_library_cache.scanned_at
                else None
            ),
            "plex_configured": bool(plex_quality_cache.checked_at or plex_quality_cache.index),
        },
    )


def _min_reduction_ratio(app) -> float:
    """The app's own `min_redownload_reduction_pct` (Settings -> App
    Detail, blank = use the built-in default) as the ratio
    find_smaller_release expects -- confirmed live, 2026-10-28, a user
    wanted this configurable per app, not a hardcoded constant.
    """
    pct = app["min_redownload_reduction_pct"]
    if pct is None:
        return MIN_SIZE_REDUCTION_RATIO
    return 1 - (pct / 100)


def _apply_redownload(
    client: arr.RadarrApp,
    root_folder_path: str,
    file: dict,
    by_path: dict,
    by_title_year: dict,
    quality_profile_id: int | None,
    min_reduction_ratio: float,
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
    `min_reduction_ratio` comes from the app's own
    `min_redownload_reduction_pct` (routes calling this compute it), not
    hardcoded here.
    """
    action, match = _classify_redownload(file, by_path, by_title_year)
    name = file.get("name") or file.get("path", "")
    current_size = file["size_bytes"]
    current_gb = current_size / 1024**3

    if action == "replace":
        if quality_profile_id is not None:
            client.set_quality_profile(match.movie_id, quality_profile_id)
        release = client.find_smaller_release(match.movie_id, current_size, min_reduction_ratio)
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
        release = client.find_smaller_release(match.movie_id, current_size, min_reduction_ratio)
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
        release = client.find_smaller_release(added.movie_id, current_size, min_reduction_ratio)
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
            _min_reduction_ratio(app),
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
                        client,
                        app["staging_root"],
                        f,
                        by_path,
                        by_title_year,
                        profile_id,
                        _min_reduction_ratio(app),
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


def _duplicates_redirect_url(*, error: str | None = None, result: str | None = None) -> str:
    params = []
    if error:
        params.append(f"error={quote(error)}")
    if result:
        params.append(f"result={quote(result)}")
    return "/log/duplicates" + ("?" + "&".join(params) if params else "")


@router.get("/log/duplicates")
def log_duplicates(request: Request, error: str | None = None, result: str | None = None):
    """Library page "Review & delete duplicates" tool -- lets the reviewer
    pick exactly which copy of each duplicate to delete, rather than
    relying on the automatic cleanup (library.py's remove_larger_duplicates,
    run from jobs/machine.py's advance_clean) which only ever fires right
    after a NEW file lands and only ever removes something strictly larger
    than it -- it never revisits duplicates already sitting in the library
    on its own, and never lets a human override its larger-only rule.

    When Plex is configured, which files actually count as duplicates of
    each other comes straight from Plex's own `duplicate=1` library
    filter (the same source its web UI's "Duplicates" view uses) instead
    of relocarr re-deriving that itself from a (title, year) filename
    regex -- confirmed live, 2026-10-28: the filename-only grouping
    missed a real duplicate sitting under an older, differently-formatted
    name (no year-bracket convention, predating relocarr itself), which
    Plex's real metadata match caught correctly. Falls back to the
    filename-based grouping only when Plex isn't configured/reachable, so
    this page still works without it.
    """
    with session(settings.db_path) as conn:
        apps = conn.execute("SELECT * FROM apps").fetchall()

    if plex_quality_cache.checked_at is not None:
        duplicate_groups = find_duplicate_groups_from_plex(
            movie_library_cache.files, plex_quality_cache.duplicate_groups
        )
    else:
        duplicate_groups = find_duplicate_groups(movie_library_cache.files)
    all_files = [f for group in duplicate_groups for f in group]
    if all_files:
        _enrich_with_radarr_matches(apps, all_files)
        # Read-only lookup against the background-cached Plex index, same
        # "never a live external call inside a request" rule as
        # disk_health/library/version_check -- see plex.py.
        _enrich_with_plex_poster_links(all_files)
        for f in all_files:
            mtime = f.get("mtime")
            f["mtime_str"] = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d") if mtime else None

    return templates.TemplateResponse(
        request,
        "log_duplicates.html",
        {
            "duplicate_groups": duplicate_groups,
            "error": error,
            "result": result,
            "plex_configured": bool(plex_quality_cache.checked_at or plex_quality_cache.index),
            "grouped_via_plex": plex_quality_cache.checked_at is not None,
        },
    )


@router.post("/log/duplicates/delete")
def log_duplicates_delete(paths: list[str] = Form(default=[])):
    """Bulk-deletes the files checked on /log/duplicates. Each `paths`
    value is "<app_id>|<path>", same convention as this page's bulk
    redownload checkboxes. A file Radarr currently tracks as that movie's
    active file is deleted through Radarr's own API (so Radarr's database
    stays consistent -- it'll correctly show the movie as missing a file
    afterward, rather than silently pointing at a file that's actually
    gone); anything else (an older file Radarr never tracked, or no
    longer tracks because it was redownloaded under a different id) is
    deleted directly from disk.

    Confirmed live, 2026-10-28: without the cache correction below, a
    just-deleted file kept showing as a duplicate for up to the next
    10-minute library-scan/Plex-check cycle on reload -- confusing, since
    the delete itself genuinely succeeded immediately. relocarr knows
    exactly which paths it just removed, so it corrects both caches
    in-place rather than waiting for either background loop, and nudges
    Plex to start its own scan the same way its "Scan Library Files"
    button does (doesn't wait for that scan to finish -- Plex's own
    duplicate list will still lag behind by however long that takes, but
    at least it's no longer only ever triggered by the user remembering
    to click it themselves).
    """
    if not paths:
        return RedirectResponse(
            _duplicates_redirect_url(error="Nothing was checked -- nothing deleted."), status_code=303
        )

    with session(settings.db_path) as conn:
        apps_by_id = {
            a["id"]: a for a in conn.execute("SELECT * FROM apps WHERE kind = 'radarr'").fetchall()
        }

    by_app: dict[int, list[str]] = {}
    for raw in paths:
        app_id_str, _, path = raw.partition("|")
        try:
            by_app.setdefault(int(app_id_str), []).append(path)
        except ValueError:
            continue

    deleted = 0
    failed = 0
    deleted_paths: list[str] = []
    for app_id, file_paths in by_app.items():
        app = apps_by_id.get(app_id)
        by_path: dict = {}
        client = None
        if app is not None:
            client = arr.RadarrApp(app["base_url"], app["api_key"])
            try:
                by_path, _ = client.build_movie_index()
            except httpx.HTTPError:
                logger.exception("could not reach Radarr app %r for duplicate cleanup", app_id)
        try:
            for path in file_paths:
                match = by_path.get(path)
                try:
                    if match is not None and client is not None:
                        client.delete_file(str(match.movie_file_id))
                    else:
                        p = Path(path)
                        if p.is_file():
                            p.unlink()
                    deleted += 1
                    deleted_paths.append(path)
                except (OSError, httpx.HTTPError):
                    logger.exception("could not delete duplicate %r", path)
                    failed += 1
        finally:
            if client is not None:
                client.close()

    if deleted_paths:
        deleted_names = {Path(p).name for p in deleted_paths}
        movie_library_cache.files = [f for f in movie_library_cache.files if f["path"] not in deleted_paths]
        for name in deleted_names:
            plex_quality_cache.index.pop(name, None)
        plex_quality_cache.duplicate_groups = [
            [f for f in group if f["name"] not in deleted_names]
            for group in plex_quality_cache.duplicate_groups
        ]
        plex_quality_cache.duplicate_groups = [
            group for group in plex_quality_cache.duplicate_groups if len(group) > 1
        ]

        with session(settings.db_path) as conn:
            plex_rows = {
                r["key"]: r["value"]
                for r in conn.execute(
                    "SELECT key, value FROM settings WHERE key IN ('plex_base_url', 'plex_token')"
                ).fetchall()
            }
        base_url, token = plex_rows.get("plex_base_url"), plex_rows.get("plex_token")
        if base_url and token:
            plex_client = plex.PlexClient(base_url, token)
            try:
                plex_client.refresh_movie_sections()
            except httpx.HTTPError:
                logger.exception("could not trigger a Plex library refresh after duplicate cleanup")
            finally:
                plex_client.close()

    result = f"Deleted {deleted} file{'s' if deleted != 1 else ''}."
    if failed:
        result += f" {failed} failed -- see the server log."
    return RedirectResponse(_duplicates_redirect_url(result=result), status_code=303)


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


def _cleanup_redirect_url(*, error: str | None = None, result: str | None = None) -> str:
    params = []
    if error:
        params.append(f"error={quote(error)}")
    if result:
        params.append(f"result={quote(result)}")
    return "/jobs/cleanup-torrents" + ("?" + "&".join(params) if params else "")


@router.get("/jobs/cleanup-torrents")
def jobs_cleanup_torrents(request: Request, error: str | None = None, result: str | None = None):
    """Jobs page "Find completed torrents" button. jobs/worker.py's removal
    gate already removes a torrent (and its data) automatically -- but only
    once every sibling job sharing that download_id has reached RECONCILED,
    and only if a download client is configured for the app. A stuck
    sibling, a torrent that was removed from Transmission out-of-band (so
    its hash no longer correlates to anything live), or a missing download
    client each leave a finished torrent sitting there indefinitely with
    nothing left to trigger the gate again. This is the manual fallback for
    whatever the gate hasn't (or can't) caught: every torrent that's both
    still present in Transmission AND backed by a DONE job for the same
    download_id -- i.e. pure leftovers, safe to mass-delete -- without
    touching anything still mid-transfer or still awaited by Radarr/Sonarr.

    "DONE" alone isn't trusted blindly, per the user's original intent
    (confirmed live, 2026-10-28): a job's state is set once and never
    revisited, so it only proves the file WAS successfully published at
    some point in the past, not that it's still there now. Each
    candidate is cross-checked against the background library scan
    (movie_library_cache) before being offered -- a DONE job whose
    target file has since vanished is logged and skipped, never shown as
    a safe-to-delete duplicate.
    """
    with session(settings.db_path) as conn:
        apps = conn.execute("SELECT * FROM apps").fetchall()
        done_jobs = conn.execute(
            "SELECT app_id, download_id, target_path, size_bytes FROM jobs "
            "WHERE state = 'DONE' AND download_id IS NOT NULL"
        ).fetchall()

    done_by_app: dict[int, dict[str, object]] = {}
    for j in done_jobs:
        done_by_app.setdefault(j["app_id"], {})[j["download_id"].lower()] = j

    current_target_paths = {f["path"] for f in movie_library_cache.files}

    candidates = []
    for app in apps:
        done = done_by_app.get(app["id"])
        if not done:
            continue
        with session(settings.db_path) as conn:
            dc = conn.execute(
                "SELECT * FROM download_clients WHERE app_id = ? LIMIT 1", (app["id"],)
            ).fetchone()
        if dc is None or dc["kind"] != "transmission":
            continue
        client = TransmissionClient(dc["host"], dc["port"], dc["username"], dc["password"])
        try:
            torrents = client.list_torrents()
        except httpx.HTTPError:
            logger.exception("could not reach Transmission for app %r", app["name"])
            continue
        finally:
            client.close()
        for t in torrents:
            job = done.get(t.hash.lower())
            if job is None:
                continue
            # Check against the background-refreshed library scan, not a
            # live stat() here -- confirmed live, 2026-10-28, this was a
            # real gap: a job's `state` is set once, when the pipeline
            # finished, and never revisited, so if the published file at
            # target_path were ever deleted or moved afterward by
            # anything else, the old DONE row alone would still call this
            # torrent a safe duplicate -- deleting it then would remove
            # what might by now be the only copy left anywhere. A live
            # Path.is_file() here would re-introduce the exact "unprotected
            # stat() call can hang a page on a wedged mount" bug already
            # fixed elsewhere (disk_health.py/library.py) -- reusing the
            # already-safe periodic scan avoids that entirely. Trade-off:
            # can be up to SCAN_INTERVAL_SECONDS (10 min) stale, same
            # staleness every other feature reading this cache accepts.
            if job["target_path"] not in current_target_paths:
                logger.warning(
                    "cleanup-torrents: job for %r is DONE but its target file is "
                    "missing from the last library scan (%r) -- not offering its "
                    "torrent as a safe duplicate",
                    t.name,
                    job["target_path"],
                )
                continue
            candidates.append(
                {
                    "app_id": app["id"],
                    "app_name": app["name"],
                    "torrent_id": t.id,
                    "name": t.name,
                    "size_bytes": job["size_bytes"],
                    "target_path": job["target_path"],
                }
            )

    return templates.TemplateResponse(
        request,
        "jobs_cleanup.html",
        {"candidates": candidates, "error": error, "result": result},
    )


@router.post("/jobs/cleanup-torrents/delete")
def jobs_cleanup_torrents_delete(torrents: list[str] = Form(default=[])):
    """Bulk-deletes the torrents checked on /jobs/cleanup-torrents, removing
    both the Transmission entry and its on-disk data. Safe specifically
    because every candidate shown there already has a DONE job -- the file
    was already copied, verified, and published elsewhere, so what's left
    in Transmission's download folder is a pure duplicate. Each `torrents`
    value is "<app_id>|<torrent_id>", same convention as the Log page's
    bulk redownload checkboxes.
    """
    if not torrents:
        return RedirectResponse(
            _cleanup_redirect_url(error="Nothing was checked -- nothing deleted."), status_code=303
        )

    with session(settings.db_path) as conn:
        apps_by_id = {a["id"]: a for a in conn.execute("SELECT * FROM apps").fetchall()}

    by_app: dict[int, list[int]] = {}
    for raw in torrents:
        app_id_str, _, torrent_id_str = raw.partition("|")
        try:
            by_app.setdefault(int(app_id_str), []).append(int(torrent_id_str))
        except ValueError:
            continue

    deleted = 0
    failed = 0
    for app_id, torrent_ids in by_app.items():
        app = apps_by_id.get(app_id)
        with session(settings.db_path) as conn:
            dc = conn.execute(
                "SELECT * FROM download_clients WHERE app_id = ? LIMIT 1", (app_id,)
            ).fetchone()
        if app is None or dc is None:
            failed += len(torrent_ids)
            continue
        client = TransmissionClient(dc["host"], dc["port"], dc["username"], dc["password"])
        try:
            for torrent_id in torrent_ids:
                try:
                    client.remove(torrent_id, delete_data=True)
                    deleted += 1
                except httpx.HTTPError:
                    logger.exception("could not remove torrent %r for app %r", torrent_id, app_id)
                    failed += 1
        finally:
            client.close()

    result = f"Deleted {deleted} torrent{'s' if deleted != 1 else ''}."
    if failed:
        result += f" {failed} failed -- see the server log."
    return RedirectResponse(_cleanup_redirect_url(result=result), status_code=303)


def _download_client_for_app(app_id: int) -> sqlite3.Row | None:
    with session(settings.db_path) as conn:
        return conn.execute(
            "SELECT * FROM download_clients WHERE app_id = ? LIMIT 1", (app_id,)
        ).fetchone()


def _source_redirect_url(*, error: str | None = None, result: str | None = None) -> str:
    params = []
    if error:
        params.append(f"error={quote(error)}")
    if result:
        params.append(f"result={quote(result)}")
    return "/jobs/cleanup-source" + ("?" + "&".join(params) if params else "")


@router.get("/jobs/cleanup-source")
def jobs_cleanup_source(request: Request, error: str | None = None, result: str | None = None):
    """Jobs page "Find transferred movies on source" button. advance_clean
    (jobs/machine.py) only ever prunes the per-movie staging folder if it's
    already empty -- it never deletes the media file itself, by design,
    because while a torrent is still tracked live in Transmission that
    file IS what Transmission is seeding from. The removal gate
    (jobs/worker.py's _advance_removal_gate) is what's actually supposed to
    delete it, by removing the torrent (with its data) once every sibling
    job for that download reaches RECONCILED -- but a stuck sibling, a
    torrent already removed from Transmission out-of-band, or no download
    client configured at all each leave the source file sitting here
    indefinitely, same gap as /jobs/cleanup-torrents.

    This finds every DONE job whose source file still physically exists,
    regardless of why, and shows whether it's still a live Transmission
    torrent (so deletion goes through Transmission's own remove -- it
    stops the torrent cleanly before touching the file) or already
    orphaned (deleted straight off disk).
    """
    with session(settings.db_path) as conn:
        apps = conn.execute("SELECT * FROM apps").fetchall()
        done_jobs = conn.execute("SELECT * FROM jobs WHERE state = 'DONE'").fetchall()
    apps_by_id = {a["id"]: a for a in apps}

    live_hashes_by_app: dict[int, set[str]] = {}
    for app in apps:
        dc = _download_client_for_app(app["id"])
        if dc is None or dc["kind"] != "transmission":
            continue
        client = TransmissionClient(dc["host"], dc["port"], dc["username"], dc["password"])
        try:
            live_hashes_by_app[app["id"]] = {t.hash.lower() for t in client.list_torrents()}
        except httpx.HTTPError:
            logger.exception("could not reach Transmission for app %r", app["name"])
        finally:
            client.close()

    candidates = []
    for job in done_jobs:
        source = Path(job["source_path"])
        try:
            if not source.is_file():
                continue
        except OSError:
            continue
        app = apps_by_id.get(job["app_id"])
        if app is None:
            continue
        download_id = job["download_id"]
        seeding = bool(
            download_id and download_id.lower() in live_hashes_by_app.get(app["id"], set())
        )
        candidates.append(
            {
                "job_id": job["id"],
                "app_name": app["name"],
                "name": source.name,
                "size_bytes": job["size_bytes"],
                "seeding": seeding,
            }
        )

    return templates.TemplateResponse(
        request,
        "jobs_cleanup_source.html",
        {"candidates": candidates, "error": error, "result": result},
    )


@router.post("/jobs/cleanup-source/delete")
def jobs_cleanup_source_delete(job_ids: list[str] = Form(default=[])):
    """Bulk-deletes the source files checked on /jobs/cleanup-source. A job
    still matching a live Transmission torrent is removed through
    Transmission's own API (delete-local-data) -- it stops the torrent
    before touching the file, rather than yanking a file out from under an
    actively seeding torrent. Everything else (no download_id, no download
    client, or the torrent's already gone from Transmission) is deleted
    directly, then its now-possibly-empty staging folder is pruned the
    same way advance_clean does.
    """
    ids = []
    for raw in job_ids:
        try:
            ids.append(int(raw))
        except ValueError:
            continue
    if not ids:
        return RedirectResponse(
            _source_redirect_url(error="Nothing was checked -- nothing deleted."), status_code=303
        )

    with session(settings.db_path) as conn:
        placeholders = ",".join("?" * len(ids))
        jobs = conn.execute(
            f"SELECT * FROM jobs WHERE id IN ({placeholders}) AND state = 'DONE'", ids
        ).fetchall()
        apps_by_id = {a["id"]: a for a in conn.execute("SELECT * FROM apps").fetchall()}

    deleted = 0
    failed = 0
    for job in jobs:
        app = apps_by_id.get(job["app_id"])
        source = Path(job["source_path"])
        removed_via_transmission = False

        if app is not None and job["download_id"]:
            dc = _download_client_for_app(app["id"])
            if dc is not None and dc["kind"] == "transmission":
                client = TransmissionClient(dc["host"], dc["port"], dc["username"], dc["password"])
                try:
                    torrent = next(
                        (
                            t
                            for t in client.list_torrents()
                            if t.hash.lower() == job["download_id"].lower()
                        ),
                        None,
                    )
                    if torrent is not None:
                        client.remove(torrent.id, delete_data=True)
                        removed_via_transmission = True
                except httpx.HTTPError:
                    logger.exception("could not remove torrent for job %r via Transmission", job["id"])
                    failed += 1
                    continue
                finally:
                    client.close()

        if not removed_via_transmission:
            try:
                if source.is_file():
                    source.unlink()
            except OSError:
                logger.exception("could not delete source file for job %r: %s", job["id"], source)
                failed += 1
                continue
            try:
                if source.parent.exists() and not any(source.parent.iterdir()):
                    source.parent.rmdir()
            except OSError:
                pass  # not empty (a sibling job's file still lives there) or already gone -- fine either way

        deleted += 1

    result = f"Deleted {deleted} file{'s' if deleted != 1 else ''} from source."
    if failed:
        result += f" {failed} failed -- see the server log."
    return RedirectResponse(_source_redirect_url(result=result), status_code=303)


def _reconcile_redirect_url(*, error: str | None = None, result: str | None = None) -> str:
    params = []
    if error:
        params.append(f"error={quote(error)}")
    if result:
        params.append(f"result={quote(result)}")
    return "/jobs/reconcile-missing" + ("?" + "&".join(params) if params else "")


@router.get("/jobs/reconcile-missing")
def jobs_reconcile_missing(request: Request, error: str | None = None, result: str | None = None):
    """Jobs page "Find & trigger missed transfers to target folder (i.e. your
    Plex library)" button. Every file relocarr
    has ever moved exists because a job was created for it -- almost always
    from Radarr's own "Download" webhook event, fired the instant Radarr
    finishes an import. If that webhook never arrives (confirmed live,
    2026-10-25: Radarr's own history showed a clean, successful import for
    two movies relocarr had zero job for, and neither file had ever been
    copied to the target -- the whole pipeline simply never started for
    them) nothing else would ever notice; there's no background scan that
    would catch a missing job on its own.

    This scans every configured Radarr app's CURRENT movie list directly
    (not relocarr's own job history, which is exactly what's incomplete
    here) for a movieFile that's both still on disk and not already
    covered by a job -- matched by media_file_id, the *arr's own moviefile
    id, the same identifier the webhook handler keys jobs on. Creating a
    job for one here is otherwise identical to what the webhook would have
    done: same target path computation, same oversized-file guard,
    download_id recovered from the movie's own grab history where
    possible (RadarrApp.get_last_download_id -- confirmed live, 2026-10-28,
    without this a backfilled job's torrent could never be found by
    /jobs/cleanup-torrents, which correlates by hash, so its leftover
    torrent would sit in Transmission forever even after a fully
    successful transfer). Falls back to NULL only when the movie genuinely
    has no grab history (a pure manual import, same as any other
    manually-imported file, PLAN.md Section 5) -- those still rely on
    /jobs/cleanup-source as the fallback instead.
    """
    with session(settings.db_path) as conn:
        apps = conn.execute("SELECT * FROM apps WHERE kind = 'radarr'").fetchall()

    candidates = []
    for app in apps:
        if not app["staging_root"] or not app["target_root"]:
            continue
        client = arr.RadarrApp(app["base_url"], app["api_key"])
        try:
            movies = client.list_movies()
        except httpx.HTTPError:
            logger.exception("could not reach Radarr app %r for reconciliation", app["name"])
            continue
        finally:
            client.close()

        with session(settings.db_path) as conn:
            tracked_file_ids = {
                row["media_file_id"]
                for row in conn.execute(
                    "SELECT media_file_id FROM jobs WHERE app_id = ?", (app["id"],)
                ).fetchall()
            }

        for m in movies:
            movie_file = m.get("movieFile")
            if not movie_file:
                continue
            file_id = str(movie_file["id"])
            if file_id in tracked_file_ids:
                continue
            try:
                compute_target_path(
                    source_path=movie_file["path"],
                    staging_root=app["staging_root"],
                    target_root=app["target_root"],
                    mode=app["layout_mode"],
                )
            except (ValueError, OSError):
                continue  # not actually under this app's staging root, or staging root unreachable
            if not Path(movie_file["path"]).is_file():
                continue  # Radarr's record is stale -- nothing there to copy
            candidates.append(
                {
                    "app_id": app["id"],
                    "app_name": app["name"],
                    "movie_id": m["id"],
                    "file_id": file_id,
                    "name": Path(movie_file["path"]).name,
                    "size_bytes": movie_file["size"],
                }
            )

    return templates.TemplateResponse(
        request,
        "jobs_reconcile.html",
        {"candidates": candidates, "error": error, "result": result},
    )


@router.post("/jobs/reconcile-missing/create")
def jobs_reconcile_missing_create(candidates: list[str] = Form(default=[])):
    """Each `candidates` value is "<app_id>|<movie_id>|<file_id>". Re-fetches
    every movie fresh from Radarr rather than trusting anything from the GET
    page -- the file could have changed (a redownload finished, a manual
    delete) in the time between viewing the list and submitting it, so the
    file_id is re-checked against what Radarr reports right now before a
    job is created.
    """
    if not candidates:
        return RedirectResponse(
            _reconcile_redirect_url(error="Nothing was checked -- nothing created."), status_code=303
        )

    with session(settings.db_path) as conn:
        apps_by_id = {a["id"]: a for a in conn.execute("SELECT * FROM apps").fetchall()}

    by_app: dict[int, set[str]] = {}
    for raw in candidates:
        app_id_str, _, rest = raw.partition("|")
        try:
            by_app.setdefault(int(app_id_str), set()).add(rest)
        except ValueError:
            continue

    created = 0
    skipped = 0
    for app_id, keys in by_app.items():
        app = apps_by_id.get(app_id)
        if app is None:
            skipped += len(keys)
            continue
        client = arr.RadarrApp(app["base_url"], app["api_key"])
        try:
            try:
                movies_by_id = {m["id"]: m for m in client.list_movies()}
            except httpx.HTTPError:
                logger.exception("could not reach Radarr app %r to create reconciled jobs", app_id)
                skipped += len(keys)
                continue

            for key in keys:
                movie_id_str, _, file_id = key.partition("|")
                try:
                    movie_id = int(movie_id_str)
                except ValueError:
                    skipped += 1
                    continue
                movie = movies_by_id.get(movie_id)
                movie_file = movie.get("movieFile") if movie else None
                if movie_file is None or str(movie_file["id"]) != file_id:
                    skipped += 1  # file changed or vanished since the list was shown
                    continue
                try:
                    target_path = compute_target_path(
                        source_path=movie_file["path"],
                        staging_root=app["staging_root"],
                        target_root=app["target_root"],
                        mode=app["layout_mode"],
                    )
                except (ValueError, OSError):
                    skipped += 1
                    continue

                max_size = app["max_file_size_bytes"]
                if max_size and movie_file["size"] > max_size:
                    initial_state = "NEEDS_ATTENTION"
                    error = f"file size {movie_file['size']} exceeds configured max {max_size}"
                else:
                    initial_state = "IMPORTED"
                    error = None

                # Confirmed live, 2026-10-28: Radarr's own history exposes
                # the originating torrent's hash on its `grabbed` event
                # (downloadId), a reliable way to recover it instead of
                # always leaving download_id NULL -- without it, a
                # backfilled job's torrent can never be found by
                # /jobs/cleanup-torrents (it correlates by hash) and sits
                # in Transmission forever even after a fully successful
                # transfer. None (no grab history at all -- a genuinely
                # manual import) is still a correct, expected outcome.
                try:
                    download_id = client.get_last_download_id(movie_id)
                except httpx.HTTPError:
                    logger.exception(
                        "could not look up download history for movie %r -- leaving download_id NULL",
                        movie_id,
                    )
                    download_id = None

                with session(settings.db_path) as conn:
                    cursor = conn.execute(
                        """INSERT OR IGNORE INTO jobs
                           (app_id, download_id, media_file_id, unmonitor_id, source_path, target_path, size_bytes, state, error)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            app_id,
                            download_id,
                            file_id,
                            movie_id,
                            movie_file["path"],
                            str(target_path),
                            movie_file["size"],
                            initial_state,
                            error,
                        ),
                    )
                created += cursor.rowcount
        finally:
            client.close()

    result = f"Created {created} job{'s' if created != 1 else ''} -- the normal pipeline will pick them up within one poll interval."
    if skipped:
        result += f" {skipped} skipped (already handled, or no longer valid)."
    return RedirectResponse(_reconcile_redirect_url(result=result), status_code=303)


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
        # "Apps configured" below only ever counts the `apps` table
        # (Radarr/Sonarr) -- confirmed live, 2026-10-28, that a user
        # reasonably read it as covering Transmission/Plex too, since
        # neither has its own row there (Transmission belongs to an app
        # as a download_clients row; Plex is a connection, not a transfer
        # destination, kept entirely out of `apps` -- see _plex_status).
        # Shown as their own About lines instead of folding them into
        # that count, so each stays accurate to what it actually means.
        download_client_count = conn.execute(
            "SELECT COUNT(*) AS n FROM download_clients"
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
    seen_devs: set[object] = set()
    disk_total_free = 0
    disk_total_total = 0
    for app in apps:
        for label, path in (("staging", app["staging_root"]), ("target", app["target_root"])):
            if not path or path in seen_paths:
                continue
            seen_paths.add(path)
            entry = disk_space_cache.entries.get(path)
            folder_entry = folder_usage_cache.entries.get(path)
            folder_used = folder_entry["used"] if folder_entry and folder_entry["ok"] else None
            if entry is None:
                disk_space.append(
                    {
                        "path": path,
                        "label": f"{app['name']} ({label})",
                        "ok": None,  # pending, not yet an error
                        "folder_used": folder_used,
                        # Total isn't known yet either, so a percentage would
                        # be meaningless here -- the folder_used GiB figure
                        # still renders on its own.
                        "folder_used_pct": None,
                    }
                )
            elif entry["ok"]:
                # Two apps can share one physical disk under different
                # subfolders (e.g. both targets land on the same NAS mount)
                # -- dev dedupes those so the total line below doesn't count
                # that disk's free/total space twice. Falls back to the path
                # itself when dev is missing (an older/manually-seeded cache
                # entry), which just forgoes dedup for that one entry rather
                # than erroring.
                dedup_key = entry.get("dev", path)
                if dedup_key not in seen_devs:
                    seen_devs.add(dedup_key)
                    disk_total_free += entry["free"]
                    disk_total_total += entry["total"]
                disk_space.append(
                    {
                        "path": path,
                        "label": f"{app['name']} ({label})",
                        "free": entry["free"],
                        "total": entry["total"],
                        "ok": True,
                        "folder_used": folder_used,
                        # What this specific folder takes up, against the whole
                        # disk it lives on -- not against its own content (a
                        # mostly-empty staging folder and a mostly-full target
                        # folder both deserve a near-zero or near-100% figure
                        # here, not a number that's only meaningful next to the
                        # other apps' folders).
                        "folder_used_pct": (
                            round(folder_used / entry["total"] * 100, 1)
                            if folder_used is not None and entry["total"]
                            else None
                        ),
                    }
                )
            else:
                disk_space.append(
                    {
                        "path": path,
                        "label": f"{app['name']} ({label})",
                        "error": entry["error"],
                        "ok": False,
                        "folder_used": folder_used,
                        "folder_used_pct": None,  # total isn't known for an unreachable path
                    }
                )

    disk_total = (
        {
            "free": disk_total_free,
            "total": disk_total_total,
            "used": disk_total_total - disk_total_free,
        }
        if disk_total_total
        else None
    )

    uptime_seconds = int(time.monotonic() - START_TIME)

    return templates.TemplateResponse(
        request,
        "system.html",
        {
            "health": health,
            "disk_space": disk_space,
            "disk_total": disk_total,
            "version": VERSION,
            "db_path": str(settings.db_path),
            "log_path": str(settings.log_path),
            "in_docker": Path("/.dockerenv").exists(),
            "uptime_seconds": uptime_seconds,
            "app_count": len(apps),
            "download_client_count": download_client_count,
            "plex_status": _plex_status(),
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
            "folder_checked_at": (
                datetime.fromtimestamp(folder_usage_cache.checked_at).strftime("%Y-%m-%d %H:%M:%S")
                if folder_usage_cache.checked_at
                else None
            ),
            "latest_version": version_check_cache.latest_version,
            "update_available": (
                is_newer(version_check_cache.latest_version, VERSION)
                if version_check_cache.latest_version
                else False
            ),
            "version_checked_at": (
                datetime.fromtimestamp(version_check_cache.checked_at).strftime("%Y-%m-%d %H:%M:%S")
                if version_check_cache.checked_at
                else None
            ),
        },
    )
