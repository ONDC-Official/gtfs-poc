"""FastAPI application: the service tier.

Tiers:
  app/api      -> transport (HTTP + websocket), no business logic
  app/services -> ingestion, analytics
  app/data     -> the data plane, behind Repository
"""
import contextlib
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import deps, routes_analytics, routes_quality, routes_realtime, routes_static, ws
from .config import settings
from .data import db
from .services.realtime import RealtimeService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S")
log = logging.getLogger("gtfs")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    deps.realtime = RealtimeService(deps.repository)
    await deps.realtime.start()
    # Analytics tier. Runs in-process because a single SQLite writer cannot be
    # shared across processes; on Postgres run_once() is the seam for lifting
    # it into its own service.
    await deps.aggregator.start()
    store = ("postgres" if settings.db_backend == "postgres"
             else settings.db_path.name)
    log.info("service ready | store=%s | poll=%ss | source=%s",
             store, settings.rt_poll_seconds,
             "live feed" if settings.rt_configured else "simulated")
    try:
        yield
    finally:
        await deps.aggregator.stop()
        if deps.realtime:
            await deps.realtime.stop()


app = FastAPI(
    title="Delhi GTFS Live",
    description="Live monitoring and analytics over Delhi bus GTFS static + realtime feeds.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(routes_static.router)
app.include_router(routes_realtime.router)
app.include_router(routes_analytics.router)
app.include_router(routes_quality.router)
app.include_router(ws.router)


@app.get("/api/health")
def health():
    rt = deps.realtime
    return {
        "status": "ok",
        "realtime": rt.status if rt else None,
        "static_loaded": deps.repository.feed_summary()["static_loaded_at"] is not None,
    }
