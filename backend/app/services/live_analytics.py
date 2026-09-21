"""Snapshot analytics: everything derivable from one fleet snapshot.

Scoped deliberately to what the feed actually carries. The Delhi feed populates
position, speed, bearing, route_id and a dispatch-encoded trip_id; it does NOT
populate stop_id, occupancy_status, congestion_level or current_status, and its
trip_id does not join to gtfs_trips. So there is no stop-arrival, crowding or
schedule-adherence metric here - those would be fabrications.

Everything below is computed in a single pass over one snapshot so the numbers
on screen are mutually consistent.
"""
import collections
import math
import time
from typing import Any, Dict, List, Optional

from ..data.repository import Repository
from .geo import haversine_m as _haversine_m
from .singleflight import SingleFlightCache

MPS_TO_KMH = 3.6

FRESH_S = 60          # reporting within the last minute
STALE_S = 300         # not seen for 5 minutes
MOVING_MPS = 0.5
# The feed clamps at 50 m/s and city buses do not sustain 72 km/h; anything
# above this is GPS noise, not a bus.
IMPLAUSIBLE_MPS = 20.0
BUNCH_M = 300.0       # two buses on one route this close are bunched
CACHE_TTL_S = 5.0


def _percentile(values: List[float], p: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return s[int(k)]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


class LiveAnalytics:
    """Computes the whole RT dashboard from one snapshot, with a short cache so
    several panels refreshing at once do not each re-scan the fleet - and a
    single-flight lock so several panels refreshing at the *same instant*
    (several open tabs, right after a cache expiry or a restart) share one
    computation instead of each starting their own."""

    def __init__(self, repo: Repository):
        self.repo = repo
        self._cache = SingleFlightCache(ttl_s=CACHE_TTL_S)

    def compute(self, force: bool = False) -> Dict[str, Any]:
        return self._cache.get_or_compute(
            "live", lambda: self._compute(time.time()), force=force)

    # -----------------------------------------------------------------------
    def _compute(self, now: float) -> Dict[str, Any]:
        rows = self.repo.latest_vehicles(None, None, None)
        total = len(rows)

        fleet = {"total": total, "fresh": 0, "moving": 0, "stopped": 0,
                 "stale": 0, "no_trip": 0}
        speeds: List[float] = []
        per_route: Dict[str, Dict[str, Any]] = {}
        dispatch = collections.Counter()
        implausible = 0
        no_position = 0

        for r in rows:
            age = now - (r.get("ts") or 0)
            speed = r.get("speed")
            stale = age > STALE_S

            if stale:
                fleet["stale"] += 1
            if age <= FRESH_S:
                fleet["fresh"] += 1
            if not r.get("trip_id"):
                fleet["no_trip"] += 1
            if r.get("lat") is None or r.get("lon") is None:
                no_position += 1

            if speed is not None:
                if speed >= IMPLAUSIBLE_MPS:
                    implausible += 1
                elif not stale:
                    speeds.append(speed)
                if not stale:
                    if speed > MOVING_MPS:
                        fleet["moving"] += 1
                    else:
                        fleet["stopped"] += 1

            rid = r.get("route_id")
            if rid and not stale:
                agg = per_route.setdefault(
                    rid, {"route_id": rid, "vehicles": 0, "speed_sum": 0.0,
                          "speed_n": 0, "moving_sum": 0.0, "moving_n": 0,
                          "points": []})
                agg["vehicles"] += 1
                if speed is not None and speed < IMPLAUSIBLE_MPS:
                    agg["speed_sum"] += speed
                    agg["speed_n"] += 1
                    if speed > MOVING_MPS:
                        agg["moving_sum"] += speed
                        agg["moving_n"] += 1
                if r.get("lat") is not None:
                    agg["points"].append((r["lat"], r["lon"]))

            trip = r.get("trip_id")
            if trip:
                parts = trip.split("_")
                # trip_id is {route}_{HH}_{MM}_{seq}; the HH is the dispatch hour
                if len(parts) == 4 and parts[1].isdigit():
                    hour = int(parts[1])
                    if 0 <= hour <= 23:
                        dispatch[hour] += 1

        bunching = self._bunching(per_route)
        names = self.repo.route_names(list(per_route.keys()))

        def decorate(rid: str) -> Dict[str, Any]:
            meta = names.get(rid) or {}
            return {"route_name": meta.get("route_short_name") or rid,
                    "route_desc": meta.get("route_desc")}

        routes = []
        for rid, agg in per_route.items():
            avg = agg["speed_sum"] / agg["speed_n"] if agg["speed_n"] else None
            mov = agg["moving_sum"] / agg["moving_n"] if agg["moving_n"] else None
            routes.append({
                "route_id": rid,
                "vehicles": agg["vehicles"],
                "moving": agg["moving_n"],
                "avg_speed_kmh": round(avg * MPS_TO_KMH, 1) if avg is not None else None,
                "moving_speed_kmh": round(mov * MPS_TO_KMH, 1) if mov is not None else None,
                "bunched": bunching["per_route"].get(rid, 0),
                **decorate(rid),
            })

        busiest = sorted(routes, key=lambda r: -r["vehicles"])[:10]
        # Congestion means buses that are running but crawling. Averaging in
        # parked vehicles just surfaces depots, so rank on moving speed with
        # enough moving buses to be more than one stuck driver.
        slow_pool = [r for r in routes
                     if r["moving"] >= 3 and r["moving_speed_kmh"] is not None]
        slowest = sorted(slow_pool, key=lambda r: r["moving_speed_kmh"])[:10]
        most_bunched = sorted([r for r in routes if r["bunched"] > 0],
                              key=lambda r: -r["bunched"])[:10]

        scheduled = self.repo.route_count()
        live_routes = len(per_route)

        return {
            "generated_at": int(now),
            "fleet": fleet,
            "speed": {
                "avg_kmh": round(sum(speeds) / len(speeds) * MPS_TO_KMH, 1) if speeds else None,
                "p50_kmh": round((_percentile(speeds, 0.50) or 0) * MPS_TO_KMH, 1) if speeds else None,
                "p85_kmh": round((_percentile(speeds, 0.85) or 0) * MPS_TO_KMH, 1) if speeds else None,
                "sample": len(speeds),
            },
            "coverage": {
                "routes_scheduled": scheduled,
                "routes_live": live_routes,
                "routes_dark": max(0, scheduled - live_routes),
                "pct_live": round(live_routes / scheduled * 100, 1) if scheduled else None,
            },
            "bunching": {
                "bunched_vehicles": bunching["vehicles"],
                "pct": round(bunching["vehicles"] / total * 100, 1) if total else None,
                "threshold_m": int(BUNCH_M),
                "routes": most_bunched,
            },
            "quality": {
                "implausible_speed": implausible,
                "implausible_pct": round(implausible / total * 100, 1) if total else None,
                "missing_trip_id": fleet["no_trip"],
                "missing_position": no_position,
                "stale": fleet["stale"],
                # Fields the spec allows but this feed never sends.
                "unpopulated_fields": ["stop_id", "occupancy_status",
                                       "congestion_level", "current_status"],
            },
            "dispatch_hours": [{"hour": h, "vehicles": dispatch.get(h, 0)} for h in range(24)],
            "busiest_routes": busiest,
            "slowest_routes": slowest,
        }

    # -----------------------------------------------------------------------
    @staticmethod
    def _bunching(per_route: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """Vehicles on the same route sitting within BUNCH_M of each other.

        Grid-hashed into BUNCH_M cells so this stays linear in fleet size
        instead of comparing every pair on a busy route.
        """
        deg = BUNCH_M / 111000.0
        per_route_counts: Dict[str, int] = {}
        total = 0

        for rid, agg in per_route.items():
            pts = agg["points"]
            if len(pts) < 2:
                continue
            cells: Dict[tuple, List[int]] = collections.defaultdict(list)
            for i, (lat, lon) in enumerate(pts):
                cells[(int(lat / deg), int(lon / deg))].append(i)

            bunched = set()
            for (cy, cx), idxs in cells.items():
                near: List[int] = []
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        near.extend(cells.get((cy + dy, cx + dx), ()))
                for i in idxs:
                    for j in near:
                        if i >= j:
                            continue
                        if _haversine_m(pts[i][0], pts[i][1], pts[j][0], pts[j][1]) < BUNCH_M:
                            bunched.add(i)
                            bunched.add(j)
            if bunched:
                per_route_counts[rid] = len(bunched)
                total += len(bunched)

        return {"vehicles": total, "per_route": per_route_counts}
