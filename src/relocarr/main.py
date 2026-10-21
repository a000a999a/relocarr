from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import settings
from .db import init_db
from .web.routes import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(settings.db_path)
    yield


app = FastAPI(title="relocarr", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="src/relocarr/web/static"), name="static")
app.include_router(router)
