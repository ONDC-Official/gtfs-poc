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
from ..data import db

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
    def __init__(self, interval_s: int = 120):
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
        conn = db.get_connection()

        if full:
            since = 0
            conn.execute("DELETE FROM analytics_grid_hour")
            conn.execute("DELETE FROM analytics_route_hour")
        else:
            row = conn.execute("SELECT value FROM meta WHERE key=?",
                               (WATERMARK_KEY,)).fetchone()
            since = max(0, int(row["value"]) - REWIND_S) if row else 0

        # Only fold buckets that have data, and align to the hour so a partial
        # bucket is replaced wholesale rather than double-counted.
        since = (since // HOUR) * HOUR
        newest = conn.execute(
            "SELECT MAX(ts) AS mx FROM rt_vehicle_position").fetchone()["mx"]
        if not newest:
            return {"rows_scanned": 0, "cells": 0, "routes": 0, "elapsed_ms": 0,
                    "watermark": since}

        scanned = conn.execute(
            "SELECT COUNT(*) AS n FROM rt_vehicle_position WHERE ts >= ?",
            (since,)).fetchone()["n"]

        # Recomputing whole buckets is what makes re-runs idempotent.
        conn.execute("DELETE FROM analytics_grid_hour WHERE hour_bucket >= ?", (since,))
        conn.execute("DELETE FROM analytics_route_hour WHERE hour_bucket >= ?", (since,))

        params = {
            "since": since, "lat_deg": GRID_LAT_DEG, "lon_deg": GRID_LON_DEG,
            "hour": HOUR, "moving": MOVING_MPS, "cap": IMPLAUSIBLE_MPS,
        }

        conn.execute("""
            INSERT INTO analytics_grid_hour (
                cell_y, cell_x, hour_bucket, observations, vehicles,
                moving, stopped, speed_sum, speed_n, speed_min, speed_max)
            SELECT
                CAST(FLOOR(lat / :lat_deg) AS INTEGER),
                CAST(FLOOR(lon / :lon_deg) AS INTEGER),
                (ts / :hour) * :hour,
                COUNT(*),
                COUNT(DISTINCT vehicle_id),
                SUM(CASE WHEN speed >  :moving AND speed < :cap THEN 1 ELSE 0 END),
                SUM(CASE WHEN speed <= :moving THEN 1 ELSE 0 END),
                SUM(CASE WHEN speed >  :moving AND speed < :cap THEN speed ELSE 0 END),
                SUM(CASE WHEN speed >  :moving AND speed < :cap THEN 1 ELSE 0 END),
                MIN(CASE WHEN speed >  :moving AND speed < :cap THEN speed END),
                MAX(CASE WHEN speed >  :moving AND speed < :cap THEN speed END)
            FROM rt_vehicle_position
            WHERE ts >= :since AND lat IS NOT NULL AND lon IS NOT NULL
            GROUP BY 1, 2, 3
        """, params)

        conn.execute("""
            INSERT INTO analytics_route_hour (
                route_id, hour_bucket, observations, vehicles,
                moving, stopped, speed_sum, speed_n)
            SELECT
                route_id,
                (ts / :hour) * :hour,
                COUNT(*),
                COUNT(DISTINCT vehicle_id),
                SUM(CASE WHEN speed >  :moving AND speed < :cap THEN 1 ELSE 0 END),
                SUM(CASE WHEN speed <= :moving THEN 1 ELSE 0 END),
                SUM(CASE WHEN speed >  :moving AND speed < :cap THEN speed ELSE 0 END),
                SUM(CASE WHEN speed >  :moving AND speed < :cap THEN 1 ELSE 0 END)
            FROM rt_vehicle_position
            WHERE ts >= :since AND route_id IS NOT NULL
            GROUP BY 1, 2
        """, params)

        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                     (WATERMARK_KEY, str(int(newest))))
        conn.commit()

        cells = conn.execute("SELECT COUNT(*) AS n FROM analytics_grid_hour").fetchone()["n"]
        routes = conn.execute("SELECT COUNT(*) AS n FROM analytics_route_hour").fetchone()["n"]
        return {
            "rows_scanned": scanned,
            "cells": cells,
            "routes": routes,
            "watermark": int(newest),
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }

    def status(self) -> Dict[str, Any]:
        conn = db.get_connection()
        row = conn.execute("SELECT value FROM meta WHERE key=?",
                           (WATERMARK_KEY,)).fetchone()
        return {
            "grid_m": int(GRID_M),
            "interval_s": self.interval_s,
            "watermark_ts": int(row["value"]) if row else None,
            "last_run": self.last_run,
            "retention_hours": settings.history_retention_hours,
        }


aggregator = Aggregator()
