"""Quality-tier endpoints: the 6-layer GTFS-realtime QA framework."""
from fastapi import APIRouter, Query

from . import deps

router = APIRouter(prefix="/api/quality", tags=["quality"])


@router.get("/coverage")
def coverage(days: float = Query(3, ge=0.25, le=30)):
    return deps.quality.coverage(days=days)


@router.get("/freshness")
def freshness(poll_window_s: int = Query(1800, ge=60, le=21600)):
    return deps.quality.freshness(poll_window_s=poll_window_s)


@router.get("/continuity")
def continuity(hours: float = Query(3, ge=0.25, le=12)):
    return deps.quality.continuity(hours=hours)


@router.get("/correctness")
def correctness(hours: float = Query(1, ge=0.1, le=12)):
    return deps.quality.correctness(hours=hours)


@router.get("/field-richness")
def field_richness(hours: float = Query(1, ge=0.1, le=12)):
    return deps.quality.field_richness(hours=hours)


@router.get("/source-reliability")
def source_reliability(hours: float = Query(24, ge=1, le=168)):
    return deps.quality.source_reliability(hours=hours)


@router.get("/summary")
def summary():
    """Everything above, one composed payload - the same "single call for the
    whole dashboard" shape as /api/analytics/live."""
    return deps.quality.summary()
