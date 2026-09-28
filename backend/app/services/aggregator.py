"""Analytics tier: the rollup writer.

Folds new realtime observations into `analytics_grid_hour` and
`analytics_route_hour`. Everything the analytics API serves is read from
those tables, never from the raw event log - which is what keeps a map that
redraws on every filter change responsive over hundreds of thousands of rows.

Incremental and idempotent. A watermark records how far the log has been
consumed; a restart resumes from there. Re-running over an already-folded
window is safe because each pass recomputes whole hour buckets rather than
adding deltas to them.

It runs inside the API process today. SQLite takes one writer at a time and
the poller is already that writer, so a second process would just contend for
the lock; the seam is `run_once()`, which is what a separate service would
call once the store is Postgres.
"""
import asyncio
import logging
import math
import time
from typing import Any, Dict, Optional

from ..config import settings
from ..data.repository import Repository
from .trip_matcher import TripMatcher

log = logging.getLogger("gtfs.aggregator")

# ~250 m cells. Longitude degrees shrink with latitude, so the lon step is
# widened by 1/cos(lat) to keep cells roughly square over Delhi rather than
# stretched into rectangles.
GRID_M = 250.0
DELHI_LAT = 28.6
GRID_LAT_DEG = GRID_M / 111_320.0
GRID_LON_DEG = GRID_M / (111_320.0 * math.cos(math.radians(DELHI_LAT)))

HOUR = 3600
MOVING_MPS = 0.5
IMPLAUSIBLE_MPS = 20.0     # feed noise; excluded from speed averages

WATERMARK_KEY = "aggregate_watermark_ts"
# Re-fold the last hour on every pass: observations for an hour keep arriving
# after the bucket opens, so the newest bucket is always incomplete.
REWIND_S = HOUR

# A vehicle silent for longer than this counts as a "reporting gap" rather
# than just the normal spacing between polls. Baked in at fold time (like
# MOVING_MPS/IMPLAUSIBLE_MPS above), not a per-request parameter - the
# continuity rollup only ever stores counts against this one threshold.
GAP_THRESHOLD_S = 90

CONTINUITY_WATERMARK_KEY = "continuity_watermark_ts"
# Unlike WATERMARK_KEY above, this one must never rewind: the continuity
# rollup is additive, so reprocessing an already-folded range double-counts.
# A stale watermark (first run against existing history, or recovering from
# downtime) is caught up incrementally instead, one bounded chunk per pass -
# see Aggregator._fold_continuity.
CONTINUITY_CHUNK_S = 86400   # at most one day of backlog per aggregator tick

TRIP_MATCH_WATERMARK_KEY = "trip_match_watermark_ts"
# Rewind a little on every pass, same reasoning as WATERMARK_KEY above: a
# vehicle mid-dispatch when its trip was first seen might report a couple more
# positions under the same trip_id, but matching only cares that the trip_id
# existed at all, so a short rewind (not an hour - trip_ids don't retroactively
# change) is enough to catch a trip whose only report so far landed right at
# the edge of the previous pass's window.
TRIP_MATCH_REWIND_S = 600
# At most this much backlog per tick, same shape as CONTINUITY_CHUNK_S - keeps
# a stale watermark (first run, or catching up after downtime) from turning
# one aggregator tick into an unbounded scan.
TRIP_MATCH_CHUNK_S = 86400


class Aggregator:
    def __init__(self, repo: Repository, interval_s: int = 120):
        self.repo = repo
        self.interval_s = interval_s
        self._task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()
        self.last_run: Optional[Dict[str, Any]] = None
        self._trip_matcher = TripMatcher(repo)

    # ---- lifecycle --------------------------------------------------------
    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="analytics-aggregator")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _loop(self) -> None:
        # Let the first poll land so the opening pass has something to fold.
        await asyncio.sleep(15)
        while not self._stopping.is_set():
            try:
                self.last_run = await asyncio.to_thread(self.run_once)
                if self.last_run["rows_scanned"]:
                    log.info("rollup: %s obs -> %s cells, %s routes in %s ms",
                             self.last_run["rows_scanned"], self.last_run["cells"],
                             self.last_run["routes"], self.last_run["elapsed_ms"])
            except Exception as exc:
                log.exception("rollup failed: %s", exc)
            try:
                await asyncio.wait_for(self._stopping.wait(), self.interval_s)
            except asyncio.TimeoutError:
                pass

    # ---- the rollup -------------------------------------------------------
    def run_once(self, full: bool = False) -> Dict[str, Any]:
        started = time.perf_counter()

        if full:
            since = 0
        else:
            mark = self.repo.meta_get(WATERMARK_KEY)
            since = max(0, int(mark) - REWIND_S) if mark else 0

        # Only fold buckets that have data, and align to the hour so a partial
        # bucket is replaced wholesale rather than double-counted.
        since = (since // HOUR) * HOUR
        newest = self.repo.newest_observation_ts()
        if not newest:
            return {"rows_scanned": 0, "cells": 0, "routes": 0, "elapsed_ms": 0,
                    "watermark": since}

        scanned = self.repo.count_observations_since(since)

        # Recomputing whole buckets is what makes re-runs idempotent. since=0
        # (a full rebuild) folds the entire log, because ts is never negative.
        self.repo.fold_rollups(since, GRID_LAT_DEG, GRID_LON_DEG,
                               HOUR, MOVING_MPS, IMPLAUSIBLE_MPS)

        # Advanced only after the fold commits: if this write is lost, the next
        # pass rewinds from the older mark and re-folds, which is harmless.
        self.repo.meta_set(WATERMARK_KEY, str(int(newest)))

        continuity = self._fold_continuity(newest)
        trip_match = self._match_new_trips(newest)

        counts = self.repo.rollup_counts()
        return {
            "rows_scanned": scanned,
            "cells": counts["cells"],
            "routes": counts["routes"],
            "watermark": int(newest),
            "continuity": continuity,
            "trip_match": trip_match,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }

    def _match_new_trips(self, newest: int) -> Dict[str, Any]:
        """One bounded step of live-trip-to-timetable matching, same shape as
        _fold_continuity above: at most TRIP_MATCH_CHUNK_S of backlog per tick,
        a small rewind rather than a full rebuild, populating live_trip_match
        for anything QualityService / an API consumer wants "is this trip
        scheduled" for, without recomputing that match inline on every read.
        """
        mark = self.repo.meta_get(TRIP_MATCH_WATERMARK_KEY)
        since = max(0, int(mark) - TRIP_MATCH_REWIND_S) if mark else max(
            0, newest - settings.history_retention_hours * 3600)
        if since >= newest:
            return {"since": since, "until": since, "matched": 0, "chunked": False}

        until = min(since + TRIP_MATCH_CHUNK_S, newest)
        live_trips = self.repo.distinct_live_trip_keys(since, until)
        if live_trips:
            now = int(time.time())
            rows = []
            for lt in live_trips:
                result = self._trip_matcher.match(lt["route_id"], lt["trip_id"])
                rows.append({
                    "live_trip_id": lt["trip_id"], "vehicle_id": lt["vehicle_id"],
                    "route_id": lt["route_id"], "matched_trip_id": result.matched_trip_id,
                    "match_type": result.match_type, "delta_minutes": result.delta_minutes,
                    "matched_at": now,
                })
            self.repo.upsert_trip_matches(rows)
        self.repo.meta_set(TRIP_MATCH_WATERMARK_KEY, str(int(until)))
        return {"since": since, "until": until, "matched": len(live_trips),
                "chunked": until < newest}

    def _fold_continuity(self, newest: int) -> Dict[str, Any]:
        """One bounded step of the continuity rollup: at most
        `CONTINUITY_CHUNK_S` of backlog, so a stale watermark (first run
        against existing history, or catching up after downtime) is worked
        off gradually across ticks rather than in one query that would
        reintroduce the cross-partition scan this rollup exists to avoid.
        `backend/scripts/backfill_continuity.py` calls the same repo method
        in a tight loop to catch up faster during a maintenance window.
        """
        mark = self.repo.meta_get(CONTINUITY_WATERMARK_KEY)
        if mark:
            since = int(mark)
        else:
            # Never run before: start at the retention boundary, not at unix
            # epoch 0. rt_vehicle_position never holds anything older than
            # history_retention_hours (prune_history), so walking forward
            # from 0 in CONTINUITY_CHUNK_S-sized steps would spend years of
            # aggregator ticks re-processing a history that no longer exists.
            since = max(0, newest - settings.history_retention_hours * 3600)
        if since >= newest:
            return {"since": since, "until": since, "chunked": False}

        until = min(since + CONTINUITY_CHUNK_S, newest)
        self.repo.fold_continuity_gaps(since, until, GAP_THRESHOLD_S)
        return {"since": since, "until": until, "chunked": until < newest}

    def status(self) -> Dict[str, Any]:
        mark = self.repo.meta_get(WATERMARK_KEY)
        continuity_mark = self.repo.meta_get(CONTINUITY_WATERMARK_KEY)
        trip_match_mark = self.repo.meta_get(TRIP_MATCH_WATERMARK_KEY)
        return {
            "grid_m": int(GRID_M),
            "interval_s": self.interval_s,
            "watermark_ts": int(mark) if mark else None,
            "continuity_watermark_ts": int(continuity_mark) if continuity_mark else None,
            "trip_match_watermark_ts": int(trip_match_mark) if trip_match_mark else None,
            "last_run": self.last_run,
            "retention_hours": settings.history_retention_hours,
        }
