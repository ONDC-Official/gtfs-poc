"""Websocket push of each poll's vehicle batch."""
import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..config import settings
from . import deps

log = logging.getLogger("gtfs.ws")

# Keepalive must outlast the poll cadence, else every poll is preceded by a ping.
PING_AFTER_S = settings.rt_poll_seconds + 15
router = APIRouter()


@router.websocket("/ws/vehicles")
async def ws_vehicles(websocket: WebSocket):
    await websocket.accept()
    rt = deps.get_realtime()
    queue = rt.subscribe()

    # Send current state immediately so a fresh client is not blank until the
    # next poll comes around.
    snapshot_routes, snapshot_vehicles = rt.wire_frame(list(rt.snapshot.values()))
    await websocket.send_json({
        "type": "snapshot",
        "status": rt.status,
        "routes": snapshot_routes,
        "vehicles": snapshot_vehicles,
    })

    try:
        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=PING_AFTER_S)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "ping"})
                continue
            await websocket.send_json(msg)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.info("websocket closed: %s", exc)
    finally:
        rt.unsubscribe(queue)
