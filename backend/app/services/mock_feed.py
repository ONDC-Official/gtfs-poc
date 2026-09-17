"""Synthetic GTFS-realtime source.

Used when no API key is configured, so the whole stack (poller -> data plane ->
websocket -> map) is demonstrable offline. Vehicles are driven along real
shapes from the static feed, so positions land on actual Delhi bus corridors
rather than random points.
"""
import math
import random
import time
from typing import Any, Dict, List

from ..data.repository import Repository

ROUTES = 80             # distinct routes to simulate
MAX_PER_ROUTE = 4
CRUISE_MPS = 8.5        # ~30 km/h, a realistic urban bus average


def _segment_lengths(pts: List[List[float]]) -> List[float]:
    """Approximate metres between consecutive [lon, lat] points."""
    out = []
    for i in range(len(pts) - 1):
        (lon1, lat1), (lon2, lat2) = pts[i], pts[i + 1]
        mlat = math.radians((lat1 + lat2) / 2.0)
        dx = math.radians(lon2 - lon1) * 6371000.0 * math.cos(mlat)
        dy = math.radians(lat2 - lat1) * 6371000.0
        out.append(math.hypot(dx, dy))
    return out


class _Track:
    def __init__(self, route_id: str, name: str, pts: List[List[float]]):
        self.route_id = route_id
        self.name = name
        self.pts = pts
        self.seg = _segment_lengths(pts)
        self.total = sum(self.seg) or 1.0

    def at(self, dist: float):
        """Position and bearing at `dist` metres along the shape, ping-ponging
        at the ends so buses run the corridor in both directions."""
        cycle = dist % (2 * self.total)
        d = cycle if cycle <= self.total else (2 * self.total - cycle)
        forward = cycle <= self.total
        acc = 0.0
        for i, seg in enumerate(self.seg):
            if acc + seg >= d:
                t = (d - acc) / seg if seg else 0.0
                lon1, lat1 = self.pts[i]
                lon2, lat2 = self.pts[i + 1]
                lon, lat = lon1 + (lon2 - lon1) * t, lat1 + (lat2 - lat1) * t
                brg = math.degrees(math.atan2(lon2 - lon1, lat2 - lat1)) % 360
                if not forward:
                    brg = (brg + 180) % 360
                return lon, lat, brg
            acc += seg
        lon, lat = self.pts[-1]
        return lon, lat, 0.0


class MockFeed:
    def __init__(self, repo: Repository, seed: int = 7):
        self.rng = random.Random(seed)
        self.tracks: List[_Track] = []
        self.vehicles: List[Dict[str, Any]] = []
        self._load(repo)

    def _load(self, repo: Repository) -> None:
        page = repo.list_routes(None, 1200, 0)
        candidates = [r for r in page["items"] if r.get("trip_count")]
        self.rng.shuffle(candidates)
        for r in candidates:
            if len(self.tracks) >= ROUTES:
                break
            pts = repo.route_shape(r["route_id"])
            if len(pts) >= 12:
                self.tracks.append(_Track(
                    r["route_id"], r.get("route_short_name") or r["route_id"], pts))

        for t in self.tracks:
            for k in range(self.rng.randint(1, MAX_PER_ROUTE)):
                self.vehicles.append({
                    "vehicle_id": "SIM-{0}-{1}".format(t.route_id, k),
                    "track": t,
                    "dist": self.rng.uniform(0, t.total),
                    "speed": CRUISE_MPS * self.rng.uniform(0.7, 1.3),
                    "dwell_until": 0.0,
                })

    @property
    def ready(self) -> bool:
        return bool(self.vehicles)

    def tick(self, now: float, elapsed: float) -> List[Dict[str, Any]]:
        rows = []
        for v in self.vehicles:
            if now < v["dwell_until"]:
                speed = 0.0                       # dwelling at a stop
            else:
                speed = max(0.0, v["speed"] + self.rng.gauss(0, 1.2))
                v["dist"] += speed * elapsed
                if self.rng.random() < 0.06:      # occasional stop
                    v["dwell_until"] = now + self.rng.uniform(15, 45)
            lon, lat, brg = v["track"].at(v["dist"])
            rows.append({
                "vehicle_id": v["vehicle_id"],
                "ts": int(now),
                "trip_id": None,
                "route_id": v["track"].route_id,
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "bearing": round(brg, 1),
                "speed": round(speed, 2),
                "stop_id": None,
                "current_status": 1 if speed == 0.0 else 2,
                "congestion_level": None,
                "occupancy_status": None,
            })
        return rows
