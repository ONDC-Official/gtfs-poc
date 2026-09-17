"""Analytics endpoints backed by the realtime history in the data plane."""
import asyncio

from fastapi import APIRouter, HTTPException, Query

from . import deps

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


@router.get("/live")
def live(force: bool = False):
    """Everything derivable from the current fleet snapshot, computed in one
    pass so every number on the dashboard agrees with every other."""
    return deps.live_analytics.compute(force=force)


@router.get("/dark-routes")
def dark_routes(limit: int = Query(25, ge=1, le=200)):
    """Scheduled routes with nothing reporting, busiest schedule first."""
    return {"items": deps.repository.dark_routes(limit)}


@router.get("/overview")
def overview(window_s: int = Query(300, ge=30, le=3600)):
    return deps.analytics.overview(window_s)


@router.get("/routes/top")
def top_routes(limit: int = Query(12, ge=1, le=100)):
    return {"items": deps.analytics.top_routes(limit)}


@router.get("/ingest")
def ingest(minutes: int = Query(60, ge=5, le=1440),
           bucket_s: int = Query(60, ge=30, le=3600)):
    return {"items": deps.analytics.ingest_series(minutes, bucket_s)}


@router.get("/speed")
def speed(minutes: int = Query(60, ge=5, le=1440),
          bins: int = Query(12, ge=4, le=24)):
    return {"items": deps.analytics.speed_distribution(minutes, bins)}


@router.get("/feed-health")
def feed_health(limit: int = Query(40, ge=1, le=500)):
    return {"items": deps.analytics.feed_health(limit)}


# ---------------------------------------------------------------------------
# Analytics tier: everything below reads the rollups, not the observation log.
# ---------------------------------------------------------------------------

@router.get("/grid")
def grid(hours: float = Query(6, ge=0.25, le=168),
         factor: int = Query(1, ge=1, le=8),
         bbox: str = Query(None, description="minLon,minLat,maxLon,maxLat"),
         min_observations: int = Query(8, ge=1, le=10000),
         limit: int = Query(20000, ge=100, le=60000)):
    """Grid rollup in a compact columnar form. `factor` scales the cell up from
    the 250 m base (2 = 500 m, 4 = 1 km) for coarser zoom levels."""
    return deps.spatial.grid(hours=hours, factor=factor,
                             bbox=deps.parse_bbox(bbox),
                             min_observations=min_observations, limit=limit)


@router.get("/hotspots")
def hotspots(hours: float = Query(6, ge=0.25, le=168),
             limit: int = Query(12, ge=1, le=100),
             min_observations: int = Query(40, ge=1, le=10000)):
    """Worst cells by congestion and dwell, and the busiest, each labelled
    with the nearest scheduled stop."""
    return deps.spatial.hotspots(hours=hours, limit=limit,
                                 min_observations=min_observations)


@router.get("/corridor/{route_id}")
def corridor(route_id: str, hours: float = Query(6, ge=0.25, le=168)):
    """Per-cell speed along one route, for drawing a speed-coloured corridor."""
    result = deps.spatial.corridor(route_id, hours=hours)
    if not result["points"]:
        raise HTTPException(404, "no observations for this route in the window")
    return result


@router.get("/hourly")
def hourly(hours: float = Query(24, ge=1, le=168)):
    return deps.spatial.hourly(hours=hours)


@router.get("/aggregator")
def aggregator_status():
    return deps.aggregator.status()


@router.post("/aggregator/run")
async def aggregator_run(full: bool = False):
    """Force a rollup pass. `full=true` rebuilds every bucket from the log."""
    result = await asyncio.to_thread(deps.aggregator.run_once, full)
    deps.aggregator.last_run = result
    return result
