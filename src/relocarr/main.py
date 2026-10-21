import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import settings
from .db import init_db
from .jobs.scheduler import run_forever
from .web.routes import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(settings.db_path)
    stop_event = asyncio.Event()
    scheduler_task = asyncio.create_task(run_forever(settings.db_path, stop_event))
    yield
    stop_event.set()
    await scheduler_task


app = FastAPI(title="relocarr", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="src/relocarr/web/static"), name="static")
app.include_router(router)
