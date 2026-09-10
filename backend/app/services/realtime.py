"""Realtime ingestion service.

Owns one background loop that, every `rt_poll_seconds`:
  1. fetches the GTFS-realtime VehiclePositions protobuf (or ticks the mock),
  2. persists observations through the data-plane port,
  3. keeps an in-memory snapshot for fast reads,
  4. broadcasts the delta to connected websocket clients.
"""
import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional

import httpx

from ..config import settings
from ..data.repository import Repository
from .mock_feed import MockFeed

log = logging.getLogger("gtfs.realtime")

_TAG = re.compile(r"<[^>]+>")

# The per-point projection of a vehicle trail. See RealtimeService.history().
TRAIL_FIELDS = ("ts", "lat", "lon", "speed", "bearing", "route_id", "trip_id")


def _tidy_error(status_code, body):
    """Gateways answer with HTML error pages; the UI shows this string in a
    banner, so reduce it to one readable line."""
    text = " ".join(_TAG.sub(" ", body or "").split())
    if status_code in (401, 403):
        return "HTTP {0} - feed rejected the API key. Check RT_API_KEY in backend/.env.".format(
            status_code)
    if status_code == 429:
        return "HTTP 429 - rate limited by the feed. Increase RT_POLL_SECONDS."
    return "HTTP {0}{1}".format(status_code, (": " + text[:160]) if text else "")

try:
    from google.transit import gtfs_realtime_pb2
except ImportError:                                  # pragma: no cover
    gtfs_realtime_pb2 = None


class RealtimeService:
    def __init__(self, repo: Repository):
        self.repo = repo
        self._task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()
        self._subscribers: List[asyncio.Queue] = []
        self._mock: Optional[MockFeed] = None
        self._last_tick = time.time()

        # in-memory current state, vehicle_id -> row
        self.snapshot: Dict[str, Dict[str, Any]] = {}
        self.status: Dict[str, Any] = {
            "source": "feed" if settings.rt_configured else "mock",
            "configured": settings.rt_configured,
            "poll_seconds": settings.rt_poll_seconds,
            "last_poll_at": None,
            "last_success_at": None,
            "last_error": None,
            "consecutive_failures": 0,
            "polls": 0,
            "vehicles": 0,
            "feed_timestamp": None,
            "latency_ms": None,
        }

    # ---- lifecycle --------------------------------------------------------
    async def start(self) -> None:
        if not settings.rt_configured and settings.rt_mock_when_unconfigured:
            log.warning("No RT_API_KEY set - serving a simulated feed. "
                        "Set it in backend/.env to use the live feed.")
            self._mock = await asyncio.to_thread(MockFeed, self.repo)
        else:
            # A previous run may have left simulated buses behind; they would
            # otherwise be counted as live vehicles.
            purged = await asyncio.to_thread(self.repo.purge_simulated)
            if purged:
                log.info("purged %s simulated rows left by a prior mock run", purged)
        self._restore_snapshot()
        self._task = asyncio.create_task(self._loop(), name="rt-poller")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    def _restore_snapshot(self) -> None:
        """Warm the in-memory state from the data plane so a restart does not
        blank the map until the first poll lands."""
        try:
            for row in self.repo.latest_vehicles(None, None, 900):
                self.snapshot[row["vehicle_id"]] = row
            self.status["vehicles"] = len(self.snapshot)
        except Exception as exc:                     # non-fatal
            log.warning("snapshot restore failed: %s", exc)

    # ---- websocket fan-out ------------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=4)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def _broadcast(self, message: Dict[str, Any]) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                # Slow client: drop the oldest frame rather than stall the poller.
                try:
                    q.get_nowait()
                    q.put_nowait(message)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    # ---- polling ----------------------------------------------------------
    async def _loop(self) -> None:
        # Give the app a moment to finish binding before the first fetch.
        await asyncio.sleep(1.0)
        prune_counter = 0
        while not self._stopping.is_set():
            started = time.perf_counter()
            try:
                await self.poll_once()
            except Exception as exc:                 # keep the loop alive
                log.exception("poll failed: %s", exc)
                self.status["last_error"] = str(exc)
                self.status["consecutive_failures"] += 1

            prune_counter += 1
            if prune_counter % 120 == 0:             # ~hourly at 30s cadence
                cutoff = int(time.time()) - settings.history_retention_hours * 3600
                try:
                    removed = await asyncio.to_thread(self.repo.prune_history, cutoff)
                    log.info("pruned %s history rows", removed)
                except Exception as exc:
                    log.warning("prune failed: %s", exc)

            elapsed = time.perf_counter() - started
            try:
                await asyncio.wait_for(self._stopping.wait(),
                                       max(1.0, settings.rt_poll_seconds - elapsed))
            except asyncio.TimeoutError:
                pass

    async def poll_once(self) -> Dict[str, Any]:
        now = time.time()
        t0 = time.perf_counter()
        source = "mock"
        http_status = None
        feed_ts = None
        error = None
        rows: List[Dict[str, Any]] = []

        if settings.rt_configured:
            source = "feed"
            rows, http_status, feed_ts, error = await self._fetch_feed()
        elif self._mock is not None and self._mock.ready:
            elapsed = max(1.0, now - self._last_tick)
            rows = self._mock.tick(now, elapsed)
        else:
            error = "no realtime source configured"

        self._last_tick = now
        latency_ms = int((time.perf_counter() - t0) * 1000)
        ingested_at = int(now)
        for r in rows:
            r["ingested_at"] = ingested_at

        new_rows = 0
        if rows:
            new_rows = await asyncio.to_thread(self.repo.upsert_vehicles, rows)
            for r in rows:
                self.snapshot[r["vehicle_id"]] = r

        ok = error is None and bool(rows)
        self.status.update({
            "source": source,
            "last_poll_at": ingested_at,
            "polls": self.status["polls"] + 1,
            "vehicles": len(self.snapshot),
            "feed_timestamp": feed_ts,
            "latency_ms": latency_ms,
        })
        if ok:
            self.status["last_success_at"] = ingested_at
            self.status["last_error"] = None
            self.status["consecutive_failures"] = 0
        else:
            self.status["last_error"] = error or "feed returned no entities"
            self.status["consecutive_failures"] += 1

        await asyncio.to_thread(
            self.repo.log_poll, polled_at=ingested_at, ok=ok, source=source,
            http_status=http_status, entity_count=len(rows), new_rows=new_rows,
            feed_timestamp=feed_ts, latency_ms=latency_ms, error=self.status["last_error"])

        routes, wire = self.wire_frame(rows) if rows else ({}, [])
        self._broadcast({
            "type": "vehicles",
            "ts": ingested_at,
            "count": len(wire),
            "status": dict(self.status),
            "routes": routes,
            "vehicles": wire,
        })
        return {"ok": ok, "entities": len(rows), "new_rows": new_rows, "error": error}

    async def _fetch_feed(self):
        """Returns (rows, http_status, feed_timestamp, error)."""
        if gtfs_realtime_pb2 is None:
            return [], None, None, "gtfs-realtime-bindings not installed"

        url = settings.rt_vehicle_positions_url
        params, headers = {}, {}
        if settings.rt_api_key:
            if settings.rt_api_key_header:
                headers[settings.rt_api_key_header] = settings.rt_api_key
            elif settings.rt_api_key_param not in url:
                params[settings.rt_api_key_param] = settings.rt_api_key

        try:
            async with httpx.AsyncClient(timeout=settings.rt_timeout_seconds,
                                         follow_redirects=True) as client:
                # httpx replaces the URL's own query string when `params` is
                # given, so only pass it when we actually have something to add.
                resp = await client.get(url, params=params or None, headers=headers)
        except httpx.HTTPError as exc:
            return [], None, None, "request failed: {0}".format(exc)

        if resp.status_code != 200:
            return [], resp.status_code, None, _tidy_error(resp.status_code, resp.text)

        feed = gtfs_realtime_pb2.FeedMessage()
        try:
            feed.ParseFromString(resp.content)
        except Exception as exc:
            # Some gateways answer 200 with an HTML error page.
            return [], resp.status_code, None, (
                "Feed did not return valid GTFS-realtime protobuf "
                "(got {0} bytes of {1}). {2}".format(
                    len(resp.content), resp.headers.get("content-type", "unknown"), exc))

        feed_ts = feed.header.timestamp or None
        rows = self._entities_to_rows(feed, feed_ts)
        return rows, resp.status_code, feed_ts, None

    @staticmethod
    def _entities_to_rows(feed, feed_ts) -> List[Dict[str, Any]]:
        rows = []
        fallback_ts = int(feed_ts or time.time())
        for ent in feed.entity:
            if not ent.HasField("vehicle"):
                continue
            v = ent.vehicle
            vid = (v.vehicle.id or v.vehicle.label or ent.id or "").strip()
            if not vid:
                continue
            pos = v.position
            rows.append({
                "vehicle_id": vid,
                "ts": int(v.timestamp or fallback_ts),
                "trip_id": v.trip.trip_id or None,
                "route_id": v.trip.route_id or None,
                "lat": pos.latitude if v.HasField("position") else None,
                "lon": pos.longitude if v.HasField("position") else None,
                "bearing": pos.bearing if v.HasField("position") else None,
                "speed": pos.speed if v.HasField("position") else None,
                "stop_id": v.stop_id or None,
                "current_status": v.current_status if v.HasField("current_status") else None,
                "congestion_level": v.congestion_level if v.HasField("congestion_level") else None,
                "occupancy_status": v.occupancy_status if v.HasField("occupancy_status") else None,
            })
        return rows

    # ---- reads ------------------------------------------------------------
    def history(self, vehicle_id: str, from_ts: int, to_ts: int) -> Dict[str, Any]:
        """One vehicle's trail over a closed time window.

        Projected to the fields that describe motion. The rest of the stored
        row is either constant for the query (vehicle_id), an ingestion detail
        (ingested_at), or one of the four fields this feed never populates -
        all of which would be dead weight repeated on every point.
        """
        rows = self.repo.vehicle_history(vehicle_id, from_ts, to_ts)
        return {
            "vehicle_id": vehicle_id,
            "from": from_ts,
            "to": to_ts,
            "count": len(rows),
            "items": [{k: r.get(k) for k in TRAIL_FIELDS} for r in rows],
            "geometry": {
                "type": "LineString",
                # Both halves must be present: a row with one coordinate null
                # would otherwise emit a malformed position.
                "coordinates": [[r["lon"], r["lat"]] for r in rows
                                if r.get("lon") is not None and r.get("lat") is not None],
            },
        }

    def decorate(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Join route names onto realtime rows for display."""
        names = self.repo.route_names([r.get("route_id") for r in rows])
        out = []
        for r in rows:
            d = dict(r)
            meta = names.get(r.get("route_id") or "")
            d["route_name"] = (meta or {}).get("route_short_name")
            d["route_desc"] = (meta or {}).get("route_desc")
            d["agency_id"] = (meta or {}).get("agency_id")
            out.append(d)
        return out

    def wire_frame(self, rows: List[Dict[str, Any]]):
        """Normalised websocket payload.

        Route metadata is sent once per frame in a lookup rather than repeated
        on every vehicle; stop_id, current_status, congestion_level and
        occupancy_status are omitted because this feed never populates them
        (they cost a fifth of the payload as nulls, and the REST shape keeps
        them for spec completeness); and coordinates are
        rounded to ~1 m. For ~5k vehicles this takes the frame from ~2.0 MB to
        ~0.8 MB, which keeps it under the 1 MiB frame limit that many
        websocket clients and proxies default to.
        """
        names = self.repo.route_names([r.get("route_id") for r in rows])
        routes: Dict[str, Dict[str, Any]] = {}
        vehicles: List[Dict[str, Any]] = []

        for r in rows:
            rid = r.get("route_id")
            if rid and rid not in routes:
                meta = names.get(rid) or {}
                routes[rid] = {
                    "name": meta.get("route_short_name"),
                    "desc": meta.get("route_desc"),
                    "agency": meta.get("agency_id"),
                }
            lat, lon = r.get("lat"), r.get("lon")
            brg, spd = r.get("bearing"), r.get("speed")
            vehicles.append({
                "vehicle_id": r["vehicle_id"],
                "ts": r["ts"],
                "route_id": rid,
                "trip_id": r.get("trip_id"),
                "lat": round(lat, 5) if lat is not None else None,
                "lon": round(lon, 5) if lon is not None else None,
                "bearing": round(brg) if brg is not None else None,
                "speed": round(spd, 1) if spd is not None else None,
            })
        return routes, vehicles

    def current(self, route_id: Optional[str] = None,
                max_age_s: Optional[int] = None) -> List[Dict[str, Any]]:
        rows = list(self.snapshot.values())
        if route_id:
            rows = [r for r in rows if r.get("route_id") == route_id]
        if max_age_s:
            cutoff = time.time() - max_age_s
            rows = [r for r in rows if (r.get("ts") or 0) >= cutoff]
        return self.decorate(rows)
