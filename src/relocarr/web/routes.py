import os
import secrets
import shutil
from pathlib import Path

from fastapi import APIRouter, Form, Header, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import arr
from ..arr.payloads import parse_radarr_webhook, parse_sonarr_webhook
from ..arr.preflight import evaluate_preflight
from ..config import settings
from ..db import session
from ..transfer.layout import compute_target_path
from ..transfer.safety import PathSafetyError, assert_distinct_roots

router = APIRouter()
templates = Jinja2Templates(directory="src/relocarr/web/templates")

_PARSERS = {"radarr": parse_radarr_webhook, "sonarr": parse_sonarr_webhook}


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


@router.get("/")
def dashboard(request: Request):
    with session(settings.db_path) as conn:
        apps = conn.execute("SELECT * FROM apps ORDER BY name").fetchall()
        open_jobs = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE state NOT IN ('DONE', 'SKIPPED')"
        ).fetchone()["n"]
    return templates.TemplateResponse(
        request, "index.html", {"apps": apps, "open_jobs": open_jobs}
    )


@router.get("/apps/new")
def apps_new_form(request: Request):
    return templates.TemplateResponse(request, "apps_new.html", {"error": None, "values": {}})


@router.post("/apps/new")
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
        conn.execute(
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
        )
    return RedirectResponse("/", status_code=303)


@router.get("/apps/{app_id}")
def app_detail(request: Request, app_id: int):
    with session(settings.db_path) as conn:
        app = conn.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
        if app is None:
            raise HTTPException(404, "unknown app")
        download_clients = conn.execute(
            "SELECT * FROM download_clients WHERE app_id = ?", (app_id,)
        ).fetchall()

    target_error = _check_target_folder(app["staging_root"], app["target_root"])
    return templates.TemplateResponse(
        request,
        "app_detail.html",
        {"app": app, "download_clients": download_clients, "target_error": target_error},
    )


@router.post("/apps/{app_id}")
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
    return RedirectResponse(f"/apps/{app_id}", status_code=303)


@router.post("/apps/{app_id}/download-client")
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
    return RedirectResponse(f"/apps/{app_id}", status_code=303)


@router.get("/apps/{app_id}/preflight")
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


@router.get("/jobs")
def jobs_list(request: Request):
    with session(settings.db_path) as conn:
        jobs = conn.execute(
            """SELECT jobs.*, apps.name AS app_name FROM jobs
               JOIN apps ON apps.id = jobs.app_id
               ORDER BY jobs.updated_at DESC LIMIT 200"""
        ).fetchall()
    return templates.TemplateResponse(request, "jobs.html", {"jobs": jobs})


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


@router.get("/settings")
def settings_form(request: Request):
    with session(settings.db_path) as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return templates.TemplateResponse(
        request, "settings.html", {"settings": {r["key"]: r["value"] for r in rows}}
    )


@router.post("/settings")
def settings_submit(
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
    return RedirectResponse("/settings", status_code=303)
