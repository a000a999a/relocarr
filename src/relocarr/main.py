import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import settings
from .db import init_db
from .disk_health import cache as disk_space_cache
from .disk_health import run_disk_space_check_forever
from .jobs.scheduler import run_forever
from .library import cache as movie_library_cache
from .library import run_library_scan_forever
from .web.csrf import csrf_guard
from .web.routes import router

# Confirmed missing in production, 2026-10-22: every logger.info() call across
# the codebase (dry-run "would do X" messages, scheduler tick failures, etc.)
# was going nowhere -- Python's root logger has no handler by default, so
# nothing below WARNING ever reached `docker logs`. Without this, dry-run
# mode -- the default, safe-by-design setting -- was completely silent,
# which defeats its whole purpose (Section 12: "logs every action it would
# take"). Uvicorn's own access logs were visible the whole time because
# uvicorn configures its own loggers separately; application logging was not.
logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(settings.db_path)
    stop_event = asyncio.Event()
    scheduler_task = asyncio.create_task(run_forever(settings.db_path, stop_event))
    library_scan_task = asyncio.create_task(
        run_library_scan_forever(settings.db_path, movie_library_cache, stop_event)
    )
    disk_space_task = asyncio.create_task(
        run_disk_space_check_forever(settings.db_path, disk_space_cache, stop_event)
    )
    yield
    stop_event.set()
    await scheduler_task
    await library_scan_task
    await disk_space_task


app = FastAPI(title="relocarr", lifespan=lifespan)
app.middleware("http")(csrf_guard)
app.mount("/static", StaticFiles(directory="src/relocarr/web/static"), name="static")
app.include_router(router)
