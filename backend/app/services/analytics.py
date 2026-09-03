"""Analytics tier.

Today it composes data-plane aggregates. When the analytics store moves to
Postgres/DuckDB this is the layer that grows (headway adherence, dwell
detection, corridor speed profiles) without the API contract changing.
"""
import time
from typing import Any, Dict, List

from ..data.repository import Repository

MPS_TO_KMH = 3.6


class AnalyticsService:
    def __init__(self, repo: Repository):
        self.repo = repo

    def overview(self, window_s: int = 300) -> Dict[str, Any]:
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

    def top_routes(self, limit: int = 12) -> List[Dict[str, Any]]:
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

    def ingest_series(self, minutes: int = 60, bucket_s: int = 60) -> List[Dict[str, Any]]:
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

    def speed_distribution(self, minutes: int = 60, bins: int = 12) -> List[Dict[str, Any]]:
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

    def feed_health(self, limit: int = 40) -> List[Dict[str, Any]]:
        return list(reversed(self.repo.recent_polls(limit)))
