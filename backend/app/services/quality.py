"""The quality tier: the 6-layer GTFS-realtime QA framework (Coverage,
Freshness & Latency, Continuity, Correctness, Field Richness, Source
Reliability).

Every layer that measures the live fleet (route coverage, freshness,
implausible speed, off-route/coordinate validity) is computed from
`Repository.vehicles_in_window()` - each vehicle's most recent observation
inside the layer's own time window - not from the single latest-ever
snapshot. That means every layer, `summary()` included, can be recomputed
for an arbitrary historical range (last 30 minutes, 1 hour, 2 hours, ...)
straight from the database, not just "right now". The handful of exceptions
that genuinely can't be windowed - schema stability (reflects only the
latest poll batch), the static feed's own metadata, and the configured
EXPECTED_FLEET_SIZE - are called out at their call sites below.

One method per layer, plus `summary()` which composes a light version of all
six for a single overview call, the same shape as `/api/analytics/live`.
"""
import statistics
import time
from typing import Any, Dict, List, Optional

from ..config import settings
from ..data.repository import Repository
from .aggregator import GAP_THRESHOLD_S, GRID_LAT_DEG, GRID_LON_DEG
from .geo import point_to_polyline_m
from .live_analytics import IMPLAUSIBLE_MPS, STALE_S

FRESH_THRESHOLDS_S = (30, 60, 120, 300)
OFF_ROUTE_M = 50.0
# Bound how many of the busiest live routes get an off-route-distance pass,
# so a big fleet can't turn one dashboard call into hundreds of shape fetches.
OFF_ROUTE_MAX_ROUTES = 150

# summary() does real CPU-bound work across all six layers (the off-route
# point-to-polyline loop especially), and Python's GIL means concurrent
# callers don't parallelize that - they serialize behind each other's CPU
# time. Prometheus scrapes /api/metrics every 30s and the frontend polls
# /api/quality/summary every 60s; without a cache, any overlap between those
# (plus a person just refreshing the page) turns into queued, increasingly
# slow requests instead of independent fast ones. Same pattern as
# LiveAnalytics.compute()'s cache, for the same reason.
SUMMARY_CACHE_TTL_S = 15.0

# summary()/metrics accept one unified `minutes` window applied to every
# layer at once (see below); cap how many distinct windows stay cached
# concurrently so an endpoint fed arbitrary query-param values can't grow
# this dict without bound over a long-running process.
MAX_CACHED_WINDOWS = 8


def _pct(n: Optional[float], total: Optional[float]) -> Optional[float]:
    if not total:
        return None
    return round((n or 0) / total * 100, 1)


class QualityService:
    def __init__(self, repo: Repository):
        self.repo = repo
        self._summary_cache: Dict[Optional[float], Dict[str, Any]] = {}
        self._summary_cache_at: Dict[Optional[float], float] = {}

    # ---- Layer 1: Coverage --------------------------------------------------
    def coverage(self, days: float = 3.0) -> Dict[str, Any]:
        now = time.time()
        since_ts = int(now - days * 86400)

        fleet_stats = self.repo.fleet_stats(STALE_S)
        active = fleet_stats.get("active_vehicles") or 0
        expected = settings.expected_fleet_size
        route_count = self.repo.route_count()
        # Routes with >=1 vehicle reporting anywhere in [since_ts, now] - not
        # just the current instant - so this reflects the same `days` window
        # as spatial/temporal coverage below, not "right now".
        window_rows = self.repo.vehicles_in_window(since_ts, int(now))
        routes_live = len({r["route_id"] for r in window_rows if r.get("route_id")})
        routes_dark = max(0, route_count - routes_live)
        # dark_route_load's ranked top-N list still reflects the current
        # snapshot (dark_routes() queries rt_vehicle_latest) - a full
        # windowed version would need a second heavier query for a display
        # list that's secondary to the routes_live/routes_dark counts above.
        dark = self.repo.dark_routes(50)

        cells_observed = self.repo.distinct_grid_cells(since_ts)
        summary = self.repo.static_feed_meta()
        bbox = summary.get("bbox")
        cells_total = None
        if bbox:
            dlat = max(bbox["max_lat"] - bbox["min_lat"], 0)
            dlon = max(bbox["max_lon"] - bbox["min_lon"], 0)
            cells_total = max(1, round((dlat / GRID_LAT_DEG) * (dlon / GRID_LON_DEG)))

        by_hour = self.repo.route_coverage_by_hour(since_ts)
        temporal = [{
            "hour": r["hod"], "weekend": bool(r["weekend"]),
            "avg_routes_live": round(r["avg_routes_live"], 1) if r["avg_routes_live"] else 0,
            "pct_live": _pct(r["avg_routes_live"], route_count),
            "samples": r["samples"],
        } for r in by_hour]

        return {
            "generated_at": int(now),
            "fleet_coverage_ratio": {
                "active": active, "expected": expected,
                "pct": _pct(active, expected),
            },
            "route_coverage": {
                "routes_scheduled": route_count,
                "routes_live": routes_live,
                "routes_dark": routes_dark,
                "pct_live": _pct(routes_live, route_count),
            },
            "dark_route_load": dark,
            "spatial_coverage": {
                "cells_observed": cells_observed, "cells_total": cells_total,
                "pct": _pct(cells_observed, cells_total), "window_days": days,
            },
            "temporal_coverage": temporal,
        }

    # ---- Layer 2: Freshness & Latency ---------------------------------------
    def freshness(self, poll_window_s: int = 1800) -> Dict[str, Any]:
        now = time.time()
        # Each vehicle's most recent report inside the window, not the
        # all-time latest - so "total" is the fleet actually active in this
        # window, and age is relative to `now` from a report that fell in it.
        rows = self.repo.vehicles_in_window(int(now - poll_window_s), int(now))
        total = len(rows)

        within = {s: 0 for s in FRESH_THRESHOLDS_S}
        staleness_buckets = {"b_0_60": 0, "b_60_180": 0, "b_180_300": 0,
                             "b_300_600": 0, "b_600_plus": 0}
        for r in rows:
            age = now - (r.get("ts") or 0)
            for s in FRESH_THRESHOLDS_S:
                if age <= s:
                    within[s] += 1
            if age <= 60:
                staleness_buckets["b_0_60"] += 1
            elif age <= 180:
                staleness_buckets["b_60_180"] += 1
            elif age <= 300:
                staleness_buckets["b_180_300"] += 1
            elif age <= 600:
                staleness_buckets["b_300_600"] += 1
            else:
                staleness_buckets["b_600_plus"] += 1

        polls = self.repo.poll_series(int(now - poll_window_s))
        ok_polls = [p for p in polls if p["ok"] and p.get("feed_timestamp")]
        feed_ages = [p["polled_at"] - p["feed_timestamp"] for p in ok_polls]
        feed_ts_seq = sorted({p["feed_timestamp"] for p in ok_polls})
        cadences = [b - a for a, b in zip(feed_ts_seq, feed_ts_seq[1:])]
        ingest_latencies = [p["latency_ms"] for p in ok_polls if p.get("latency_ms") is not None]

        return {
            "generated_at": int(now),
            "fresh_within_s": {
                str(s): {"count": within[s], "pct": _pct(within[s], total)}
                for s in FRESH_THRESHOLDS_S
            },
            "per_vehicle_staleness": staleness_buckets,
            "feed_age_at_source_s": (
                round(sum(feed_ages) / len(feed_ages), 1) if feed_ages else None),
            "native_publish_cadence_s": (
                round(sum(cadences) / len(cadences), 1) if cadences else None),
            "end_to_end_latency_ms": {
                "onboard_to_source_s": None,  # not observable from this side
                "publish_delay_s": (
                    round(sum(feed_ages) / len(feed_ages), 1) if feed_ages else None),
                "ingest_ms": (
                    round(sum(ingest_latencies) / len(ingest_latencies))
                    if ingest_latencies else None),
            },
        }

    # ---- Layer 3: Continuity -------------------------------------------------
    def continuity(self, hours: float = 3.0) -> Dict[str, Any]:
        now = int(time.time())
        since_ts = int(now - hours * 3600)
        report = self.repo.continuity_report(since_ts, now)
        trips = self.repo.trip_completeness(since_ts, now)

        interval_s = max(settings.rt_poll_seconds, 1)
        # Postgres' AVG() over bigint columns returns Decimal (via psycopg),
        # not float like SQLite always does - coerce explicitly so the
        # mixed-type arithmetic below doesn't blow up on one backend only.
        avg_span = float(report.get("avg_span_s") or 0)
        avg_obs = float(report.get("avg_observations_per_vehicle") or 0)
        expected_slots = (avg_span / interval_s) + 1 if avg_span else None
        continuity_ratio = (
            round(min(100.0, avg_obs / expected_slots * 100), 1)
            if expected_slots else None)

        return {
            "generated_at": now, "window_hours": hours,
            "report_continuity_pct": continuity_ratio,
            "vehicles": report.get("vehicles") or 0,
            "gap_frequency": {
                "gaps_over_threshold": report.get("gaps_over_threshold") or 0,
                # Baked in at rollup time (aggregator.GAP_THRESHOLD_S), not a
                # per-request choice - see fold_continuity_gaps.
                "threshold_s": GAP_THRESHOLD_S,
                "pct_of_gaps": _pct(report.get("gaps_over_threshold"), report.get("gaps_total")),
            },
            "gap_duration_distribution": report.get("gap_buckets") or {},
            "trip_completeness": {
                "trips": trips.get("trips") or 0,
                "complete_trips": trips.get("complete_trips") or 0,
                "pct": _pct(trips.get("complete_trips"), trips.get("trips")),
            },
            # Every gap over the threshold is, by definition, a stale->recovered
            # cycle - the same count doubles as the churn signal (3.4).
            "session_churn": report.get("gaps_over_threshold") or 0,
        }

    # ---- Layer 4: Correctness ------------------------------------------------
    def correctness(self, hours: float = 1.0) -> Dict[str, Any]:
        now = time.time()
        since_ts = int(now - hours * 3600)
        # Each vehicle's most recent report inside [since_ts, now] - implausible
        # speed, off-route distance and coordinate validity are all computed
        # from this window, not from whatever the very latest snapshot is.
        rows = self.repo.vehicles_in_window(since_ts, int(now))
        total = len(rows)
        implausible = sum(
            1 for r in rows if (r.get("speed") or 0) >= IMPLAUSIBLE_MPS)

        summary = self.repo.static_feed_meta()
        bbox = summary.get("bbox")
        out_of_bounds = zero_coord = 0
        per_route: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            lat, lon = r.get("lat"), r.get("lon")
            if lat is None or lon is None:
                continue
            if lat == 0 and lon == 0:
                zero_coord += 1
            if bbox and not (bbox["min_lat"] <= lat <= bbox["max_lat"]
                             and bbox["min_lon"] <= lon <= bbox["max_lon"]):
                out_of_bounds += 1
            rid = r.get("route_id")
            if rid:
                per_route.setdefault(rid, []).append(r)

        busiest = sorted(per_route.items(), key=lambda kv: -len(kv[1]))[:OFF_ROUTE_MAX_ROUTES]
        within_50m = sampled = 0
        distances: List[float] = []
        for rid, vs in busiest:
            shape = self.repo.route_shape(rid)
            if not shape:
                continue
            for r in vs:
                d = point_to_polyline_m(r["lat"], r["lon"], shape)
                if d is None:
                    continue
                sampled += 1
                distances.append(d)
                if d <= OFF_ROUTE_M:
                    within_50m += 1

        ref = self.repo.referential_integrity(since_ts)
        polls = self.repo.poll_series(since_ts)
        counted_polls = [p for p in polls if p["ok"]]
        dup_polls = [p for p in counted_polls if (p.get("new_rows") or 0) == 0]

        return {
            "generated_at": int(now),
            "implausible_speed": implausible,
            "implausible_speed_pct": _pct(implausible, total),
            "off_route": {
                "sampled": sampled, "within_50m": within_50m,
                "pct_within_50m": _pct(within_50m, sampled),
                "avg_distance_m": round(sum(distances) / len(distances), 1) if distances else None,
                "routes_sampled": len(busiest),
            },
            "coordinate_validity": {
                "total": total, "out_of_bounds": out_of_bounds, "zero_coord": zero_coord,
            },
            "referential_integrity": {
                "total": ref.get("total") or 0,
                "invalid_route_id": ref.get("invalid_route_id") or 0,
                "invalid_trip_id": ref.get("invalid_trip_id") or 0,
            },
            "duplicate_snapshot_rate": {
                "polls": len(counted_polls), "duplicates": len(dup_polls),
                "pct": _pct(len(dup_polls), len(counted_polls)),
            },
        }

    # ---- Layer 5: Field Richness ---------------------------------------------
    def field_richness(self, hours: float = 1.0) -> Dict[str, Any]:
        now = time.time()
        since_ts = int(now - hours * 3600)
        fields = self.repo.field_population(since_ts)
        summary = self.repo.static_feed_meta()
        loaded_at = summary.get("static_loaded_at")

        return {
            "generated_at": int(now),
            "field_population": {
                field: {**stats, "pct": _pct(stats["populated"], stats["total"])}
                for field, stats in fields.items()
            },
            "static_feed": {
                "routes": summary.get("routes"), "stops": summary.get("stops"),
                "trips": summary.get("trips"), "shape_points": summary.get("shape_points"),
                "static_loaded_at": loaded_at,
                "age_days": (round((now - loaded_at) / 86400, 1)
                            if loaded_at else None),
            },
        }

    # ---- Layer 6: Source Reliability ------------------------------------------
    def source_reliability(self, hours: float = 24.0) -> Dict[str, Any]:
        now = time.time()
        since_ts = int(now - hours * 3600)
        polls = self.repo.poll_series(since_ts)
        total = len(polls)
        ok = [p for p in polls if p["ok"]]

        error_kinds: Dict[str, int] = {}
        for p in polls:
            if p["ok"]:
                continue
            status = p.get("http_status")
            err = (p.get("error") or "").lower()
            if "timeout" in err or "timed out" in err:
                kind = "timeout"
            elif status and status >= 500:
                kind = "5xx"
            elif status and status >= 400:
                kind = "4xx"
            elif (p.get("entity_count") or 0) == 0:
                kind = "empty_body"
            else:
                kind = "other"
            error_kinds[kind] = error_kinds.get(kind, 0) + 1

        counts = [p.get("entity_count") or 0 for p in ok]
        mean = statistics.mean(counts) if counts else None
        stdev = statistics.pstdev(counts) if len(counts) > 1 else 0.0

        return {
            "generated_at": int(now), "window_hours": hours,
            "poll_success_rate": {
                "polls": total, "ok": len(ok), "pct": _pct(len(ok), total),
            },
            "error_breakdown": error_kinds,
            "feed_volume_stability": {
                "mean_entities": round(mean, 1) if mean is not None else None,
                "stdev_entities": round(stdev, 1),
                "min_entities": min(counts) if counts else None,
                "max_entities": max(counts) if counts else None,
                # A poll under half the running mean is a sharp drop, not
                # just normal off-peak variation - flag it for a human look.
                "unstable": bool(mean and counts and min(counts) < mean * 0.5),
            },
            "schema_stability": {
                "fields": (self.repo.meta_get("rt_schema_fields") or "").split(",")
                          if self.repo.meta_get("rt_schema_fields") else [],
                "last_changed_at": (
                    int(v) if (v := self.repo.meta_get("rt_schema_changed_at")) else None),
            },
        }

    # ---- overview -------------------------------------------------------------
    def summary(self, force: bool = False, minutes: Optional[float] = None) -> Dict[str, Any]:
        """`minutes`, when given, is one time-range applied to every layer at
        once (coverage's `days`, freshness's `poll_window_s`, continuity's/
        correctness's/field_richness's/source_reliability's `hours` - all
        derived from the same window) so "last 30 minutes" or "last 2 hours"
        means the same range everywhere in the response. None keeps each
        layer's own existing default window, unchanged."""
        now = time.time()
        cached = self._summary_cache.get(minutes)
        cached_at = self._summary_cache_at.get(minutes, 0.0)
        if not force and cached is not None and now - cached_at < SUMMARY_CACHE_TTL_S:
            return cached

        if minutes is not None:
            days = minutes / 1440.0
            hours = minutes / 60.0
            poll_window_s = max(1, int(minutes * 60))
            coverage = self.coverage(days=days)
            freshness = self.freshness(poll_window_s=poll_window_s)
            continuity = self.continuity(hours=hours)
            correctness = self.correctness(hours=hours)
            field_richness = self.field_richness(hours=hours)
            source_reliability = self.source_reliability(hours=hours)
        else:
            coverage = self.coverage()
            freshness = self.freshness()
            continuity = self.continuity()
            correctness = self.correctness()
            field_richness = self.field_richness()
            source_reliability = self.source_reliability()

        # A simple, documented heuristic - not an authoritative index. Each
        # sub-score is already a 0-100 percentage; missing inputs (e.g. no
        # EXPECTED_FLEET_SIZE configured) are left out of the average rather
        # than counted as failures.
        sub_scores = [
            coverage["route_coverage"]["pct_live"],
            freshness["fresh_within_s"]["60"]["pct"],
            continuity["report_continuity_pct"],
            100 - (correctness["implausible_speed_pct"] or 0),
            source_reliability["poll_success_rate"]["pct"],
        ]
        present = [s for s in sub_scores if s is not None]
        composite = round(sum(present) / len(present), 1) if present else None

        result = {
            "generated_at": int(now),
            "window_minutes": minutes,
            "composite_qos_score": composite,
            "coverage": coverage, "freshness": freshness, "continuity": continuity,
            "correctness": correctness, "field_richness": field_richness,
            "source_reliability": source_reliability,
        }
        self._summary_cache[minutes] = result
        self._summary_cache_at[minutes] = now
        if len(self._summary_cache) > MAX_CACHED_WINDOWS:
            oldest = min(self._summary_cache_at, key=self._summary_cache_at.get)
            self._summary_cache.pop(oldest, None)
            self._summary_cache_at.pop(oldest, None)
        return result
