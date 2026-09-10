"""Realtime endpoints: current vehicles, feed status, per-vehicle history."""
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

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
def vehicle_history(
    vehicle_id: str,
    from_ts: Optional[int] = Query(
        None, alias="from", ge=0,
        description="Window start, unix seconds, inclusive."),
    to_ts: Optional[int] = Query(
        None, alias="to", ge=0,
        description="Window end, unix seconds, inclusive."),
    minutes: int = Query(60, ge=1, le=1440,
                         description="Trailing window. Ignored when from/to are given."),
):
    """One vehicle's trail. `from`/`to` take precedence over `minutes`.

    Only what the retention window still holds is returned, so a `from` older
    than HISTORY_RETENTION_HOURS yields whatever survived pruning.
    """
    if (from_ts is None) != (to_ts is None):
        raise HTTPException(422, "from and to must be supplied together")
    if from_ts is None:
        to_ts = int(time.time())
        from_ts = to_ts - minutes * 60
    elif from_ts > to_ts:
        raise HTTPException(422, "from must not be later than to")

    result = deps.get_realtime().history(vehicle_id, from_ts, to_ts)
    if not result["count"]:
        raise HTTPException(404, "no observations for this vehicle in the window")
    return result


@router.post("/poll")
async def force_poll():
    """Trigger an out-of-band poll. Handy while wiring up a new feed key."""
    return await deps.get_realtime().poll_once()
