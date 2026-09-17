"""Static GTFS endpoints: routes, stops, shapes, schedules."""
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from . import deps

router = APIRouter(prefix="/api", tags=["static"])


@router.get("/feed")
def feed_summary():
    return deps.repository.feed_summary()


@router.get("/routes")
def list_routes(q: Optional[str] = None,
                limit: int = Query(50, ge=1, le=500),
                offset: int = Query(0, ge=0)):
    return deps.repository.list_routes(q, limit, offset)


# MUST stay above /routes/{route_id}: FastAPI matches in declaration order, so
# below it this path binds as route_id="nearby" and 404s with no error to trace.
@router.get("/routes/nearby")
def routes_nearby(lat: float = Query(..., ge=-90, le=90),
                  lon: float = Query(..., ge=-180, le=180),
                  radius_m: int = Query(500, ge=1, le=5000),
                  limit: int = Query(50, ge=1, le=200)):
    """Routes serving any stop within `radius_m` of the point, nearest first.

    An empty `items` means nothing is in range - that is an answer, not an
    error. The 503 is the different case: nothing can be in range because the
    schedule was never loaded.
    """
    if deps.repository.route_count() == 0:
        raise HTTPException(
            503, "static GTFS feed not loaded - run backend/scripts/load_static.py "
                 "before calling this endpoint")
    items = deps.repository.routes_nearby(lat, lon, radius_m, limit)
    return {"lat": lat, "lon": lon, "radius_m": radius_m,
            "count": len(items), "items": items}


@router.get("/routes/{route_id}")
def get_route(route_id: str):
    route = deps.repository.get_route(route_id)
    if not route:
        raise HTTPException(404, "route not found")
    return route


@router.get("/routes/{route_id}/shape")
def route_shape(route_id: str):
    coords = deps.repository.route_shape(route_id)
    if not coords:
        raise HTTPException(404, "no shape for route")
    return {"type": "Feature",
            "properties": {"route_id": route_id},
            "geometry": {"type": "LineString", "coordinates": coords}}


@router.get("/routes/{route_id}/stops")
def route_stops(route_id: str):
    return {"items": deps.repository.route_stops(route_id)}


@router.get("/stops")
def list_stops(q: Optional[str] = None, bbox: Optional[str] = None,
               limit: int = Query(500, ge=1, le=5000)):
    return {"items": deps.repository.list_stops(q, deps.parse_bbox(bbox), limit)}


@router.get("/stops/{stop_id}/schedule")
def stop_schedule(stop_id: str,
                  from_s: int = Query(0, ge=0, le=200000),
                  to_s: int = Query(200000, ge=0, le=200000),
                  limit: int = Query(50, ge=1, le=500)):
    return {"items": deps.repository.stop_schedule(stop_id, from_s, to_s, limit)}
