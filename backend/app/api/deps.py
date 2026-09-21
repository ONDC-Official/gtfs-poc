"""Wiring between the API layer and the services it depends on.

`main.py` populates these at startup; routers pull from here so they never
construct services themselves.
"""
from typing import Optional

from ..config import settings
from ..data.repository import Repository
from ..services.analytics import AnalyticsService
from ..services.aggregator import Aggregator
from ..services.live_analytics import LiveAnalytics
from ..services.quality import QualityService
from ..services.realtime import RealtimeService
from ..services.spatial_analytics import SpatialAnalytics

def _load_repository() -> Repository:
    """Pick the data-plane adapter from DB_BACKEND. The postgres import is
    deferred so a SQLite-only install never needs psycopg."""
    if settings.db_backend == "postgres":
        from ..data.postgres_repo import repository as repo
    else:
        from ..data.sqlite_repo import repository as repo
    return repo


repository: Repository = _load_repository()
analytics = AnalyticsService(repository)
live_analytics = LiveAnalytics(repository)
spatial = SpatialAnalytics(repository)
aggregator = Aggregator(repository, interval_s=settings.aggregator_interval_s)
quality = QualityService(repository)
realtime: Optional[RealtimeService] = None


def get_realtime() -> RealtimeService:
    if realtime is None:
        raise RuntimeError("realtime service not started")
    return realtime


def parse_bbox(bbox: Optional[str]):
    """'minLon,minLat,maxLon,maxLat' -> tuple, or None."""
    if not bbox:
        return None
    parts = [p.strip() for p in bbox.split(",")]
    if len(parts) != 4:
        return None
    try:
        return tuple(float(p) for p in parts)
    except ValueError:
        return None
