import asyncio
import logging
from contextlib import asynccontextmanager
from logging.handlers import TimedRotatingFileHandler

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import settings
from .db import init_db
from .disk_health import cache as disk_space_cache
from .disk_health import folder_usage_cache
from .disk_health import run_disk_space_check_forever
from .disk_health import run_folder_usage_scan_forever
from .jobs.scheduler import run_forever, run_seal_forever
from .library import cache as movie_library_cache
from .library import run_library_scan_forever
from .plex import cache as plex_quality_cache
from .plex import run_plex_quality_check_forever
from .version_check import cache as version_check_cache
from .version_check import run_version_check_forever
from .web.csrf import csrf_guard
from .web.routes import router

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(config_settings=settings) -> None:
    """Confirmed missing in production, 2026-10-22: every logger.info() call
    across the codebase (dry-run "would do X" messages, scheduler tick
    failures, etc.) was going nowhere -- Python's root logger has no handler
    by default, so nothing below WARNING ever reached `docker logs`. Without
    this, dry-run mode -- the default, safe-by-design setting -- was
    completely silent, which defeats its whole purpose (Section 12: "logs
    every action it would take"). Uvicorn's own access logs were visible the
    whole time because uvicorn configures its own loggers separately;
    application logging was not.

    `docker logs` alone turned out not to be enough either -- confirmed
    live, 2026-10-28: investigating a real production anomaly (a job that
    reached DONE with an apparently incomplete transfer) needed logs from
    three days earlier, and they were gone. Docker's own log file is scoped
    to the CURRENT container instance; every redeploy (`docker compose up
    -d`, `./install.sh`, or recovering from the zombie-container issue this
    project has hit repeatedly) replaces the container and silently drops
    all prior history with it. `/config` is the one directory that's
    bind-mounted and actually survives a redeploy (it's where the sqlite DB
    already lives), so a second handler writes there too -- daily rotation,
    7 days kept, oldest dropped automatically. Console logging (and
    therefore `docker logs`) is kept as well, for a live tail; this is
    additive, not a replacement.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        config_settings.log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            TimedRotatingFileHandler(
                config_settings.log_path, when="midnight", backupCount=7, encoding="utf-8"
            )
        )
    except OSError:
        # config_dir not writable/doesn't exist outside the real container
        # (confirmed live, 2026-10-28: running the test suite directly on
        # the host, not inside Docker, hits exactly this against the
        # default /config) -- console logging alone is still better than
        # crashing before the app can even start.
        pass

    logging.basicConfig(
        level=config_settings.log_level,
        format=LOG_FORMAT,
        handlers=handlers,
        force=True,
    )


configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(settings.db_path)
    stop_event = asyncio.Event()
    scheduler_task = asyncio.create_task(run_forever(settings.db_path, stop_event))
    seal_task = asyncio.create_task(run_seal_forever(settings.db_path, stop_event))
    library_scan_task = asyncio.create_task(
        run_library_scan_forever(settings.db_path, movie_library_cache, stop_event)
    )
    disk_space_task = asyncio.create_task(
        run_disk_space_check_forever(settings.db_path, disk_space_cache, stop_event)
    )
    folder_usage_task = asyncio.create_task(
        run_folder_usage_scan_forever(settings.db_path, folder_usage_cache, stop_event)
    )
    version_check_task = asyncio.create_task(run_version_check_forever(version_check_cache, stop_event))
    plex_quality_task = asyncio.create_task(
        run_plex_quality_check_forever(settings.db_path, plex_quality_cache, stop_event)
    )
    yield
    stop_event.set()
    await scheduler_task
    await seal_task
    await library_scan_task
    await disk_space_task
    await folder_usage_task
    await version_check_task
    await plex_quality_task


app = FastAPI(title="relocarr", lifespan=lifespan)
app.middleware("http")(csrf_guard)
app.mount("/static", StaticFiles(directory="src/relocarr/web/static"), name="static")
app.include_router(router)
