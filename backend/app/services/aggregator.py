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


class Aggregator:
    def __init__(self, repo: Repository, interval_s: int = 120):
        self.repo = repo
        self.interval_s = interval_s
        self._task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()
        self.last_run: Optional[Dict[str, Any]] = None

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

        counts = self.repo.rollup_counts()
        return {
            "rows_scanned": scanned,
            "cells": counts["cells"],
            "routes": counts["routes"],
            "watermark": int(newest),
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }

    def status(self) -> Dict[str, Any]:
        mark = self.repo.meta_get(WATERMARK_KEY)
        return {
            "grid_m": int(GRID_M),
            "interval_s": self.interval_s,
            "watermark_ts": int(mark) if mark else None,
            "last_run": self.last_run,
            "retention_hours": settings.history_retention_hours,
        }
