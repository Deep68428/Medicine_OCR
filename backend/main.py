from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from core.logging import setup_logging
from core.deps import close_remote_db, close_local_db
from middlewares.cros import setup_cors
from middlewares.logging import logging_middleware
from middlewares.request_id import request_id_middleware
from middlewares.timing import timing_middleware
from routes.config import router as config_router
from routes.picknote import router as picknote_router

setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await close_remote_db()
    await close_local_db()


app = FastAPI(lifespan=lifespan)

setup_cors(app)

app.middleware("http")(logging_middleware)
app.middleware("http")(request_id_middleware)
app.middleware("http")(timing_middleware)

app.include_router(config_router)
app.include_router(picknote_router)


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
