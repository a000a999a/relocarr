import os
import secrets
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, Form, Header, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import VERSION, arr
from ..arr.payloads import parse_radarr_webhook, parse_sonarr_webhook
from ..arr.preflight import evaluate_preflight
from ..config import settings
from ..db import session
from ..transfer.layout import compute_target_path
from ..transfer.safety import PathSafetyError, assert_distinct_roots

router = APIRouter()
templates = Jinja2Templates(directory="src/relocarr/web/templates")

_PARSERS = {"radarr": parse_radarr_webhook, "sonarr": parse_sonarr_webhook}

START_TIME = time.monotonic()


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


@router.post("/settings/apps/{app_id}/download-client")
def add_download_client(
    request: Request,
    app_id: int,
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
    return RedirectResponse(f"/settings/apps/{app_id}", status_code=303)


@router.post("/settings/apps/{app_id}/download-client/{dc_id}/delete")
def delete_download_client(app_id: int, dc_id: int):
    with session(settings.db_path) as conn:
        conn.execute(
            "DELETE FROM download_clients WHERE id = ? AND app_id = ?", (dc_id, app_id)
        )
    return RedirectResponse(f"/settings/apps/{app_id}", status_code=303)


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


@router.get("/log")
def transfer_log(request: Request):
    """Historical record of finished transfer attempts -- success
    (DONE), failed (NEEDS_ATTENTION), or skipped (SKIPPED) -- each with the
    timestamp it reached that state. In-flight jobs (IMPORTED through
    RECONCILED) don't appear here; see /jobs for those.
    """
    with session(settings.db_path) as conn:
        entries = conn.execute(
            """SELECT jobs.*, apps.name AS app_name FROM jobs
               JOIN apps ON apps.id = jobs.app_id
               WHERE jobs.state IN ('DONE', 'NEEDS_ATTENTION', 'SKIPPED')
               ORDER BY jobs.updated_at DESC LIMIT 500"""
        ).fetchall()
    return templates.TemplateResponse(request, "log.html", {"entries": entries})


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

    disk_space = []
    seen_paths: set[str] = set()
    for app in apps:
        for label, path in (("staging", app["staging_root"]), ("target", app["target_root"])):
            if not path or path in seen_paths:
                continue
            seen_paths.add(path)
            try:
                usage = shutil.disk_usage(path)
                disk_space.append(
                    {
                        "path": path,
                        "label": f"{app['name']} ({label})",
                        "free": usage.free,
                        "total": usage.total,
                        "ok": True,
                    }
                )
            except OSError as exc:
                disk_space.append(
                    {"path": path, "label": f"{app['name']} ({label})", "error": str(exc), "ok": False}
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
        },
    )
