"""Analytics tier.

Today it composes data-plane aggregates. When the analytics store moves to
Postgres/DuckDB this is the layer that grows (headway adherence, dwell
detection, corridor speed profiles) without the API contract changing.
"""
import time
from typing import Any, Dict, List

from ..data.repository import Repository
from .singleflight import SingleFlightCache

MPS_TO_KMH = 3.6

# The Live tab polls overview/ingest/speed/feed-health every 15-30s from
# every open tab, independently. None of that data changes meaningfully
# faster than this, and it's what LiveAnalytics/QualityService already do
# for their own endpoints (5s and 15s respectively) - this was the one
# analytics-tier service without a cache, so every poll from every tab hit
# the raw observation log fresh.
CACHE_TTL_S = 10.0


class AnalyticsService:
    def __init__(self, repo: Repository):
        self.repo = repo
        # SingleFlightCache, not a plain dict: several tabs polling the same
        # endpoint at the exact instant a key is cold (a restart, or a TTL
        # expiry) share one computation instead of each re-scanning the raw
        # observation log - see singleflight.py's docstring for why that
        # distinction matters here.
        self._cache = SingleFlightCache(ttl_s=CACHE_TTL_S)

    def _cached(self, key: str, compute) -> Any:
        return self._cache.get_or_compute(key, compute)

    def overview(self, window_s: int = 300) -> Dict[str, Any]:
        def compute():
            stats = self.repo.fleet_stats(window_s)
            avg = stats.get("avg_speed")
            polls = self.repo.recent_polls(20)
            ok = [p for p in polls if p["ok"]]
            return {
                "active_vehicles": stats.get("active_vehicles") or 0,
                "active_routes": stats.get("active_routes") or 0,
                "moving": stats.get("moving") or 0,
                "stale_vehicles": stats.get("stale_vehicles") or 0,
                "avg_speed_kmh": round(avg * MPS_TO_KMH, 1) if avg else None,
                "newest_ts": stats.get("newest_ts"),
                "window_s": window_s,
                "poll_success_rate": round(len(ok) / len(polls), 3) if polls else None,
                "avg_poll_latency_ms": (
                    round(sum(p["latency_ms"] or 0 for p in ok) / len(ok)) if ok else None),
            }
        return self._cached(f"overview:{window_s}", compute)

    def top_routes(self, limit: int = 12) -> List[Dict[str, Any]]:
        def compute():
            out = []
            for r in self.repo.active_routes(limit):
                out.append({
                    "route_id": r["route_id"],
                    "route_name": r.get("route_short_name") or r["route_id"],
                    "route_desc": r.get("route_desc"),
                    "vehicles": r["vehicles"],
                    "avg_speed_kmh": (round(r["avg_speed"] * MPS_TO_KMH, 1)
                                      if r.get("avg_speed") is not None else None),
                })
            return out
        return self._cached(f"top_routes:{limit}", compute)

    def ingest_series(self, minutes: int = 60, bucket_s: int = 60) -> List[Dict[str, Any]]:
        def compute():
            since = int(time.time()) - minutes * 60
            return [
                {
                    "bucket": r["bucket"],
                    "observations": r["observations"],
                    "vehicles": r["vehicles"],
                    "avg_speed_kmh": (round(r["avg_speed"] * MPS_TO_KMH, 1)
                                      if r.get("avg_speed") is not None else None),
                }
                for r in self.repo.ingest_timeseries(bucket_s, since)
            ]
        # since=int(time.time())-... moves every call, so the cache key is
        # rounded to the TTL - two polls in the same 10s window share it,
        # a poll in the next window recomputes with a fresh `since`.
        bucket = int(time.time() // CACHE_TTL_S)
        return self._cached(f"ingest_series:{minutes}:{bucket_s}:{bucket}", compute)

    def speed_distribution(self, minutes: int = 60, bins: int = 12) -> List[Dict[str, Any]]:
        def compute():
            since = int(time.time()) - minutes * 60
            raw = {r["bin"]: r["n"] for r in self.repo.speed_histogram(since, bins)}
            out = []
            for b in range(bins):
                lo = round(b * 2 * MPS_TO_KMH)
                hi = round((b + 1) * 2 * MPS_TO_KMH)
                out.append({
                    "bin": b,
                    "label": ("{0}+".format(lo) if b == bins - 1 else "{0}-{1}".format(lo, hi)),
                    "count": raw.get(b, 0),
                })
            return out
        bucket = int(time.time() // CACHE_TTL_S)
        return self._cached(f"speed_distribution:{minutes}:{bins}:{bucket}", compute)

    def feed_health(self, limit: int = 40) -> List[Dict[str, Any]]:
        return self._cached(f"feed_health:{limit}",
                             lambda: list(reversed(self.repo.recent_polls(limit))))
