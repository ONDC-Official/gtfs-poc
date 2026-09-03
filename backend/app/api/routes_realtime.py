"""Realtime endpoints: current vehicles, feed status, per-vehicle history."""
import time
from typing import Optional

from fastapi import APIRouter, Query

from . import deps

router = APIRouter(prefix="/api/realtime", tags=["realtime"])


@router.get("/status")
def status():
    return deps.get_realtime().status


@router.get("/vehicles")
def vehicles(route_id: Optional[str] = None,
             max_age_s: Optional[int] = Query(None, ge=1, le=86400),
             bbox: Optional[str] = None):
    rt = deps.get_realtime()
    rows = rt.current(route_id=route_id, max_age_s=max_age_s)
    box = deps.parse_bbox(bbox)
    if box:
        rows = [r for r in rows
                if r.get("lon") is not None
                and box[0] <= r["lon"] <= box[2] and box[1] <= r["lat"] <= box[3]]
    return {"ts": int(time.time()), "count": len(rows), "items": rows,
            "status": rt.status}


@router.get("/vehicles/{vehicle_id}/history")
def vehicle_history(vehicle_id: str, minutes: int = Query(60, ge=1, le=1440)):
    since = int(time.time()) - minutes * 60
    rows = deps.repository.vehicle_history(vehicle_id, since)
    return {"vehicle_id": vehicle_id, "count": len(rows), "items": rows,
            "geometry": {"type": "LineString",
                         "coordinates": [[r["lon"], r["lat"]] for r in rows
                                         if r.get("lon") is not None]}}


@router.post("/poll")
async def force_poll():
    """Trigger an out-of-band poll. Handy while wiring up a new feed key."""
    return await deps.get_realtime().poll_once()
