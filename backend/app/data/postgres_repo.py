"""PostgreSQL + PostGIS implementation of the data-plane port.

Method-for-method with sqlite_repo.py; the Repository contract suite runs
against both. Where the two engines diverge silently rather than erroring, the
Postgres side is written to match SQLite's answer:

  * LIKE  -> ILIKE                (SQLite LIKE folds ASCII case, Postgres does not)
  * CAST(x AS INTEGER) -> FLOOR(x)::int   (Postgres CAST rounds; we want floor)
  * conn.total_changes -> cursor.rowcount after executemany

Spatial queries use PostGIS: routes_nearby and nearest_stop filter on
ST_DWithin against the generated geom column, and routes_nearby reports exact
geodesic metres via ST_Distance on geography.
"""
import time
from typing import Any, Dict, List, Optional, Sequence

from . import pg
from .repository import Repository

_VEHICLE_COLS = (
    "vehicle_id, ts, trip_id, route_id, lat, lon, bearing, speed, stop_id, "
    "current_status, congestion_level, occupancy_status, ingested_at"
)
_VEHICLE_PLACEHOLDERS = "(" + ",".join(["%s"] * 13) + ")"

# Postgres tolerates 65535 bind parameters; keep a conservative chunk so a
# pathological interchange with thousands of serving stops still fits.
_PARAM_CHUNK = 1000


def _vehicle_values(rows: List[Dict[str, Any]]):
    return [
        (r["vehicle_id"], r["ts"], r.get("trip_id"), r.get("route_id"),
         r.get("lat"), r.get("lon"), r.get("bearing"), r.get("speed"),
         r.get("stop_id"), r.get("current_status"), r.get("congestion_level"),
         r.get("occupancy_status"), r["ingested_at"])
        for r in rows
    ]


class PostgresRepository(Repository):
    # ---- helpers --------------------------------------------------------------
    @staticmethod
    def _all(sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        with pg.pool().connection() as conn:
            return conn.execute(sql, params).fetchall()

    @staticmethod
    def _one(sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        with pg.pool().connection() as conn:
            return conn.execute(sql, params).fetchone()

    # ---- static ------------------------------------------------------------
    def static_feed_meta(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        with pg.pool().connection() as conn:
            for table, key in (
                ("gtfs_agency", "agencies"), ("gtfs_routes", "routes"),
                ("gtfs_stops", "stops"), ("gtfs_trips", "trips"),
                ("gtfs_stop_times", "stop_times"), ("gtfs_shapes", "shape_points"),
            ):
                out[key] = conn.execute(
                    "SELECT COUNT(*) AS n FROM " + table).fetchone()["n"]
            bbox = conn.execute(
                "SELECT MIN(stop_lat) AS min_lat, MIN(stop_lon) AS min_lon, "
                "MAX(stop_lat) AS max_lat, MAX(stop_lon) AS max_lon FROM gtfs_stops"
            ).fetchone()
            out["bbox"] = dict(bbox) if bbox and bbox["min_lat"] is not None else None
            loaded = conn.execute(
                "SELECT value FROM meta WHERE key = 'static_loaded_at'").fetchone()
            out["static_loaded_at"] = int(loaded["value"]) if loaded else None
        return out

    def feed_summary(self) -> Dict[str, Any]:
        out = self.static_feed_meta()
        out["history_rows"] = self._one(
            "SELECT COUNT(*) AS n FROM rt_vehicle_position")["n"]
        return out

    def list_routes(self, q: Optional[str], limit: int, offset: int) -> Dict[str, Any]:
        where, params = "", []
        if q:
            where = ("WHERE route_short_name ILIKE %s OR route_long_name ILIKE %s "
                     "OR route_desc ILIKE %s")
            params = ["%" + q + "%"] * 3
        total = self._one(
            "SELECT COUNT(*) AS n FROM gtfs_routes " + where, params)["n"]
        items = self._all(
            "SELECT r.*, (SELECT COUNT(*) FROM gtfs_trips t WHERE t.route_id = r.route_id) "
            "AS trip_count FROM gtfs_routes r " + where +
            " ORDER BY route_short_name LIMIT %s OFFSET %s", params + [limit, offset])
        return {"total": total, "items": items}

    def get_route(self, route_id: str) -> Optional[Dict[str, Any]]:
        return self._one(
            "SELECT r.*, (SELECT COUNT(*) FROM gtfs_trips t WHERE t.route_id = r.route_id) "
            "AS trip_count FROM gtfs_routes r WHERE r.route_id = %s", (route_id,))

    def route_shape(self, route_id: str) -> List[List[float]]:
        shape = self._one(
            "SELECT s.shape_id FROM gtfs_shapes s "
            "WHERE s.shape_id IN (SELECT DISTINCT shape_id FROM gtfs_trips WHERE route_id = %s) "
            "GROUP BY s.shape_id ORDER BY COUNT(*) DESC LIMIT 1", (route_id,))
        if not shape:
            return []
        pts = self._all(
            "SELECT shape_pt_lon, shape_pt_lat FROM gtfs_shapes WHERE shape_id = %s "
            "ORDER BY shape_pt_sequence", (shape["shape_id"],))
        return [[p["shape_pt_lon"], p["shape_pt_lat"]] for p in pts]

    def route_stops(self, route_id: str) -> List[Dict[str, Any]]:
        trip = self._one(
            "SELECT st.trip_id FROM gtfs_stop_times st "
            "JOIN gtfs_trips t ON t.trip_id = st.trip_id WHERE t.route_id = %s "
            "GROUP BY st.trip_id ORDER BY COUNT(*) DESC LIMIT 1", (route_id,))
        if not trip:
            return []
        return self._all(
            "SELECT s.stop_id, s.stop_name, s.stop_lat, s.stop_lon, st.stop_sequence, "
            "st.arrival_time, st.departure_time FROM gtfs_stop_times st "
            "JOIN gtfs_stops s ON s.stop_id = st.stop_id WHERE st.trip_id = %s "
            "ORDER BY st.stop_sequence", (trip["trip_id"],))

    def list_stops(self, q: Optional[str], bbox: Optional[Sequence[float]],
                   limit: int) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if q:
            clauses.append("(stop_name ILIKE %s OR stop_code ILIKE %s)")
            params += ["%" + q + "%"] * 2
        if bbox:
            clauses.append("stop_lon BETWEEN %s AND %s AND stop_lat BETWEEN %s AND %s")
            params += [bbox[0], bbox[2], bbox[1], bbox[3]]
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return self._all(
            "SELECT stop_id, stop_code, stop_name, stop_lat, stop_lon FROM gtfs_stops "
            + where + " LIMIT %s", params + [limit])

    def stop_schedule(self, stop_id: str, from_s: int, to_s: int,
                      limit: int) -> List[Dict[str, Any]]:
        return self._all(
            "SELECT st.trip_id, st.arrival_time, st.departure_time, st.departure_s, "
            "t.route_id, r.route_short_name, r.route_desc "
            "FROM gtfs_stop_times st "
            "JOIN gtfs_trips t ON t.trip_id = st.trip_id "
            "LEFT JOIN gtfs_routes r ON r.route_id = t.route_id "
            "WHERE st.stop_id = %s AND st.departure_s BETWEEN %s AND %s "
            "ORDER BY st.departure_s LIMIT %s", (stop_id, from_s, to_s, limit))

    def routes_nearby(self, lat: float, lon: float, radius_m: float,
                      limit: int) -> List[Dict[str, Any]]:
        pt = "ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography"
        with pg.pool().connection() as conn:
            near_rows = conn.execute(
                "SELECT stop_id, stop_name, "
                "ST_Distance(geom::geography, " + pt + ") AS d "
                "FROM gtfs_stops "
                "WHERE geom IS NOT NULL AND ST_DWithin(geom::geography, " + pt + ", %s)",
                (lon, lat, lon, lat, radius_m),
            ).fetchall()
            if not near_rows:
                return []
            near = {r["stop_id"]: (r["stop_name"], r["d"]) for r in near_rows}

            stop_ids = list(near)
            out: Dict[str, Dict[str, Any]] = {}
            for i in range(0, len(stop_ids), _PARAM_CHUNK):
                chunk = stop_ids[i:i + _PARAM_CHUNK]
                marks = ",".join(["%s"] * len(chunk))
                rows = conn.execute(
                    "SELECT DISTINCT st.stop_id, t.route_id, ro.route_short_name, "
                    "ro.route_long_name, ro.agency_id "
                    "FROM gtfs_stop_times st "
                    "JOIN gtfs_trips t ON t.trip_id = st.trip_id "
                    "LEFT JOIN gtfs_routes ro ON ro.route_id = t.route_id "
                    "WHERE st.stop_id IN (" + marks + ")", chunk,
                ).fetchall()
                for row in rows:
                    rid = row["route_id"]
                    if not rid:
                        continue
                    stop_name, dist = near[row["stop_id"]]
                    entry = out.get(rid)
                    if entry is None:
                        entry = out[rid] = {
                            "route_id": rid,
                            "short_name": row["route_short_name"],
                            "long_name": row["route_long_name"],
                            "agency_id": row["agency_id"],
                            "stops": [],
                            "distance_m": dist,
                        }
                    elif dist < entry["distance_m"]:
                        entry["distance_m"] = dist
                    if stop_name and stop_name not in entry["stops"]:
                        entry["stops"].append(stop_name)

        items = sorted(out.values(), key=lambda e: e["distance_m"])[:limit]
        for e in items:
            e["distance_m"] = round(e["distance_m"], 1)
        return items

    def route_names(self, route_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        ids = [r for r in route_ids if r]
        if not ids:
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        with pg.pool().connection() as conn:
            for i in range(0, len(ids), _PARAM_CHUNK):
                chunk = ids[i:i + _PARAM_CHUNK]
                marks = ",".join(["%s"] * len(chunk))
                for r in conn.execute(
                    "SELECT route_id, route_short_name, route_long_name, route_desc, agency_id "
                    "FROM gtfs_routes WHERE route_id IN (" + marks + ")", chunk,
                ).fetchall():
                    out[r["route_id"]] = dict(r)
        return out

    # ---- realtime ---------------------------------------------------------
    def upsert_vehicles(self, rows: List[Dict[str, Any]]) -> int:
        if not rows:
            return 0
        values = _vehicle_values(rows)
        days = {(r["ts"] // 86400) * 86400 for r in rows}
        with pg.pool().connection() as conn:
            with conn.transaction():
                # Make sure a dated partition exists for every day this batch
                # touches; the DEFAULT partition catches anything that slips.
                for day_ts in days:
                    conn.execute("SELECT rt_ensure_day_partition(%s)", (day_ts,))

                with conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO rt_vehicle_position (" + _VEHICLE_COLS + ") "
                        "VALUES " + _VEHICLE_PLACEHOLDERS + " "
                        "ON CONFLICT (vehicle_id, ts) DO NOTHING", values)
                    # rowcount after executemany is the total across every param
                    # set; ON CONFLICT DO NOTHING contributes 0 for a dup, so
                    # this is the count of genuinely new history rows.
                    inserted = cur.rowcount

                    cur.executemany(
                        "INSERT INTO rt_vehicle_latest (" + _VEHICLE_COLS + ") "
                        "VALUES " + _VEHICLE_PLACEHOLDERS + " "
                        "ON CONFLICT (vehicle_id) DO UPDATE SET "
                        "ts = EXCLUDED.ts, trip_id = EXCLUDED.trip_id, "
                        "route_id = EXCLUDED.route_id, lat = EXCLUDED.lat, "
                        "lon = EXCLUDED.lon, bearing = EXCLUDED.bearing, "
                        "speed = EXCLUDED.speed, stop_id = EXCLUDED.stop_id, "
                        "current_status = EXCLUDED.current_status, "
                        "congestion_level = EXCLUDED.congestion_level, "
                        "occupancy_status = EXCLUDED.occupancy_status, "
                        "ingested_at = EXCLUDED.ingested_at "
                        "WHERE rt_vehicle_latest.ts <= EXCLUDED.ts", values)
        return max(inserted, 0)

    def latest_vehicles(self, route_id: Optional[str],
                        bbox: Optional[Sequence[float]],
                        max_age_s: Optional[int]) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if route_id:
            clauses.append("route_id = %s")
            params.append(route_id)
        if bbox:
            clauses.append("lon BETWEEN %s AND %s AND lat BETWEEN %s AND %s")
            params += [bbox[0], bbox[2], bbox[1], bbox[3]]
        if max_age_s:
            clauses.append("ts >= %s")
            params.append(int(time.time()) - max_age_s)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return self._all(
            "SELECT " + _VEHICLE_COLS + " FROM rt_vehicle_latest " + where, params)

    def vehicle_history(self, vehicle_id: str, since_ts: int,
                        until_ts: Optional[int] = None) -> List[Dict[str, Any]]:
        clauses = ["vehicle_id = %s", "ts >= %s"]
        params: List[Any] = [vehicle_id, since_ts]
        if until_ts is not None:
            clauses.append("ts <= %s")
            params.append(until_ts)
        return self._all(
            "SELECT " + _VEHICLE_COLS + " FROM rt_vehicle_position "
            "WHERE " + " AND ".join(clauses) + " ORDER BY ts", params)

    def log_poll(self, **kw: Any) -> None:
        with pg.pool().connection() as conn:
            conn.execute(
                "INSERT INTO rt_poll_log (polled_at, ok, source, http_status, "
                "entity_count, new_rows, feed_timestamp, latency_ms, error) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (kw.get("polled_at", int(time.time())), 1 if kw.get("ok") else 0,
                 kw.get("source", "feed"), kw.get("http_status"), kw.get("entity_count"),
                 kw.get("new_rows"), kw.get("feed_timestamp"), kw.get("latency_ms"),
                 kw.get("error")))

    def recent_polls(self, limit: int) -> List[Dict[str, Any]]:
        return self._all(
            "SELECT * FROM rt_poll_log ORDER BY polled_at DESC LIMIT %s", (limit,))

    def prune_history(self, older_than_ts: int) -> int:
        with pg.pool().connection() as conn:
            with conn.transaction():
                deleted = conn.execute(
                    "DELETE FROM rt_vehicle_position WHERE ts < %s",
                    (older_than_ts,)).rowcount
                conn.execute("DELETE FROM rt_poll_log WHERE polled_at < %s",
                             (older_than_ts,))
                # O(1), bloat-free half of retention: whole days below the
                # cutoff go as DROP TABLE. The DELETE above mops up the
                # boundary day and the default partition.
                conn.execute("SELECT rt_drop_old_partitions(%s)", (older_than_ts,))
        return deleted

    def purge_simulated(self) -> int:
        with pg.pool().connection() as conn:
            with conn.transaction():
                n = conn.execute(
                    "DELETE FROM rt_vehicle_latest WHERE vehicle_id LIKE 'SIM-%'"
                ).rowcount
                conn.execute(
                    "DELETE FROM rt_vehicle_position WHERE vehicle_id LIKE 'SIM-%'")
        return n

    def route_count(self) -> int:
        return self._one("SELECT COUNT(*) AS n FROM gtfs_routes")["n"]

    def dark_routes(self, limit: int) -> List[Dict[str, Any]]:
        return self._all(
            "SELECT r.route_id, r.route_short_name, r.route_desc, "
            "(SELECT COUNT(*) FROM gtfs_trips t WHERE t.route_id = r.route_id) AS trip_count "
            "FROM gtfs_routes r "
            "WHERE r.route_id NOT IN (SELECT DISTINCT route_id FROM rt_vehicle_latest "
            "                         WHERE route_id IS NOT NULL) "
            "ORDER BY trip_count DESC LIMIT %s", (limit,))

    # ---- analytics --------------------------------------------------------
    def fleet_stats(self, window_s: int) -> Dict[str, Any]:
        cutoff = int(time.time()) - window_s
        with pg.pool().connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS active_vehicles, "
                "COUNT(DISTINCT route_id) AS active_routes, "
                "AVG(speed) AS avg_speed, MAX(ts) AS newest_ts, MIN(ts) AS oldest_ts "
                "FROM rt_vehicle_latest WHERE ts >= %s", (cutoff,)).fetchone()
            stats = dict(row) if row else {}
            stats["moving"] = conn.execute(
                "SELECT COUNT(*) AS n FROM rt_vehicle_latest "
                "WHERE ts >= %s AND speed > 0.5", (cutoff,)).fetchone()["n"]
            stats["stale_vehicles"] = conn.execute(
                "SELECT COUNT(*) AS n FROM rt_vehicle_latest WHERE ts < %s",
                (cutoff,)).fetchone()["n"]
        stats["window_s"] = window_s
        return stats

    def active_routes(self, limit: int) -> List[Dict[str, Any]]:
        return self._all(
            "SELECT v.route_id, COUNT(*) AS vehicles, AVG(v.speed) AS avg_speed, "
            "r.route_short_name, r.route_desc "
            "FROM rt_vehicle_latest v LEFT JOIN gtfs_routes r ON r.route_id = v.route_id "
            "WHERE v.route_id IS NOT NULL "
            "GROUP BY v.route_id, r.route_short_name, r.route_desc "
            "ORDER BY vehicles DESC LIMIT %s", (limit,))

    def ingest_timeseries(self, bucket_s: int, since_ts: int) -> List[Dict[str, Any]]:
        return self._all(
            "SELECT (ts / %s) * %s AS bucket, COUNT(*) AS observations, "
            "COUNT(DISTINCT vehicle_id) AS vehicles, AVG(speed) AS avg_speed "
            "FROM rt_vehicle_position WHERE ts >= %s "
            "GROUP BY bucket ORDER BY bucket", (bucket_s, bucket_s, since_ts))

    def speed_histogram(self, since_ts: int, buckets: int) -> List[Dict[str, Any]]:
        # 2 m/s bins, clamped into the top bin. FLOOR, not CAST: Postgres CAST
        # rounds (2.5 -> 3) where SQLite truncates (2.5 -> 2).
        return self._all(
            "SELECT LEAST(FLOOR(speed / 2.0)::int, %s) AS bin, COUNT(*) AS n "
            "FROM rt_vehicle_position WHERE ts >= %s AND speed IS NOT NULL "
            "GROUP BY bin ORDER BY bin", (buckets - 1, since_ts))

    # ---- analytics tier (rollups) -----------------------------------------
    @staticmethod
    def _grid_bounds(bbox, lat_deg, lon_deg):
        return (int(bbox[1] / lat_deg) - 1, int(bbox[3] / lat_deg) + 1,
                int(bbox[0] / lon_deg) - 1, int(bbox[2] / lon_deg) + 1)

    def grid_cells(self, since_ts: int, factor: int,
                   bbox: Optional[Sequence[float]], min_observations: int,
                   limit: int) -> List[Dict[str, Any]]:
        from ..services.aggregator import GRID_LAT_DEG, GRID_LON_DEG
        clauses, params = ["hour_bucket >= %s"], [since_ts]
        if bbox:
            y0, y1, x0, x1 = self._grid_bounds(bbox, GRID_LAT_DEG, GRID_LON_DEG)
            clauses.append("cell_y BETWEEN %s AND %s AND cell_x BETWEEN %s AND %s")
            params += [y0, y1, x0, x1]
        where = " AND ".join(clauses)
        return self._all(
            "SELECT cell_y / %s AS gy, cell_x / %s AS gx, "
            "SUM(observations) AS observations, MAX(vehicles) AS peak_vehicles, "
            "SUM(moving) AS moving, SUM(stopped) AS stopped, "
            "SUM(speed_sum) AS speed_sum, SUM(speed_n) AS speed_n, "
            "MIN(speed_min) AS speed_min, MAX(speed_max) AS speed_max "
            "FROM analytics_grid_hour WHERE " + where +
            " GROUP BY gy, gx HAVING SUM(observations) >= %s "
            "ORDER BY observations DESC LIMIT %s",
            [factor, factor] + params + [min_observations, limit])

    def grid_extent(self, since_ts: int) -> Dict[str, Any]:
        row = self._one(
            "SELECT MAX(observations) AS max_obs, "
            "MIN(CASE WHEN speed_n > 0 THEN speed_sum / speed_n END) AS min_speed, "
            "MAX(CASE WHEN speed_n > 0 THEN speed_sum / speed_n END) AS max_speed, "
            "COUNT(*) AS cell_hours, MIN(hour_bucket) AS first_hour, "
            "MAX(hour_bucket) AS last_hour "
            "FROM analytics_grid_hour WHERE hour_bucket >= %s", (since_ts,))
        return dict(row) if row else {}

    def nearest_stop(self, lat: float, lon: float,
                     radius_deg: float) -> Optional[Dict[str, Any]]:
        # PostGIS: ST_DWithin against the GiST-indexed geom (SRID-4326 units are
        # degrees, so radius_deg carries over unchanged), nearest by KNN.
        pt = "ST_SetSRID(ST_MakePoint(%s, %s), 4326)"
        return self._one(
            "SELECT stop_id, stop_name, stop_lat, stop_lon "
            "FROM gtfs_stops "
            "WHERE geom IS NOT NULL AND ST_DWithin(geom, " + pt + ", %s) "
            "ORDER BY geom <-> " + pt + " LIMIT 1",
            (lon, lat, radius_deg, lon, lat))

    def route_corridor(self, route_id: str, since_ts: int) -> List[Dict[str, Any]]:
        from ..services.aggregator import (GRID_LAT_DEG, GRID_LON_DEG,
                                           IMPLAUSIBLE_MPS, MOVING_MPS)
        return self._all(
            "SELECT FLOOR(lat / %s)::int AS gy, FLOOR(lon / %s)::int AS gx, "
            "COUNT(*) AS observations, AVG(lat) AS lat, AVG(lon) AS lon, "
            "SUM(CASE WHEN speed > %s AND speed < %s THEN speed ELSE 0 END) AS speed_sum, "
            "SUM(CASE WHEN speed > %s AND speed < %s THEN 1 ELSE 0 END) AS speed_n, "
            "SUM(CASE WHEN speed <= %s THEN 1 ELSE 0 END) AS stopped "
            "FROM rt_vehicle_position "
            "WHERE route_id = %s AND ts >= %s AND lat IS NOT NULL "
            "GROUP BY gy, gx ORDER BY observations DESC",
            (GRID_LAT_DEG, GRID_LON_DEG, MOVING_MPS, IMPLAUSIBLE_MPS,
             MOVING_MPS, IMPLAUSIBLE_MPS, MOVING_MPS, route_id, since_ts))

    # ---- analytics tier (the rollup write path) ---------------------------
    def meta_get(self, key: str) -> Optional[str]:
        row = self._one("SELECT value FROM meta WHERE key = %s", (key,))
        return row["value"] if row else None

    def meta_set(self, key: str, value: str) -> None:
        with pg.pool().connection() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (key, value))

    def newest_observation_ts(self) -> Optional[int]:
        row = self._one("SELECT MAX(ts) AS mx FROM rt_vehicle_position")
        return int(row["mx"]) if row and row["mx"] is not None else None

    def count_observations_since(self, since_ts: int) -> int:
        return self._one(
            "SELECT COUNT(*) AS n FROM rt_vehicle_position WHERE ts >= %s",
            (since_ts,))["n"]

    def fold_rollups(self, since_ts: int, lat_deg: float, lon_deg: float,
                     hour_s: int, moving_mps: float, cap_mps: float) -> None:
        params = {"since": since_ts, "lat_deg": lat_deg, "lon_deg": lon_deg,
                  "hour": hour_s, "moving": moving_mps, "cap": cap_mps}
        with pg.pool().connection() as conn:
            with conn.transaction():   # a half-applied fold would under-report
                conn.execute(
                    "DELETE FROM analytics_grid_hour WHERE hour_bucket >= %(since)s", params)
                conn.execute(
                    "DELETE FROM analytics_route_hour WHERE hour_bucket >= %(since)s", params)

                conn.execute("""
                    INSERT INTO analytics_grid_hour (
                        cell_y, cell_x, hour_bucket, observations, vehicles,
                        moving, stopped, speed_sum, speed_n, speed_min, speed_max)
                    SELECT
                        FLOOR(lat / %(lat_deg)s)::int,
                        FLOOR(lon / %(lon_deg)s)::int,
                        (ts / %(hour)s) * %(hour)s,
                        COUNT(*),
                        COUNT(DISTINCT vehicle_id),
                        SUM(CASE WHEN speed >  %(moving)s AND speed < %(cap)s THEN 1 ELSE 0 END),
                        SUM(CASE WHEN speed <= %(moving)s THEN 1 ELSE 0 END),
                        SUM(CASE WHEN speed >  %(moving)s AND speed < %(cap)s THEN speed ELSE 0 END),
                        SUM(CASE WHEN speed >  %(moving)s AND speed < %(cap)s THEN 1 ELSE 0 END),
                        MIN(CASE WHEN speed >  %(moving)s AND speed < %(cap)s THEN speed END),
                        MAX(CASE WHEN speed >  %(moving)s AND speed < %(cap)s THEN speed END)
                    FROM rt_vehicle_position
                    WHERE ts >= %(since)s AND lat IS NOT NULL AND lon IS NOT NULL
                    GROUP BY 1, 2, 3
                """, params)

                conn.execute("""
                    INSERT INTO analytics_route_hour (
                        route_id, hour_bucket, observations, vehicles,
                        moving, stopped, speed_sum, speed_n)
                    SELECT
                        route_id,
                        (ts / %(hour)s) * %(hour)s,
                        COUNT(*),
                        COUNT(DISTINCT vehicle_id),
                        SUM(CASE WHEN speed >  %(moving)s AND speed < %(cap)s THEN 1 ELSE 0 END),
                        SUM(CASE WHEN speed <= %(moving)s THEN 1 ELSE 0 END),
                        SUM(CASE WHEN speed >  %(moving)s AND speed < %(cap)s THEN speed ELSE 0 END),
                        SUM(CASE WHEN speed >  %(moving)s AND speed < %(cap)s THEN 1 ELSE 0 END)
                    FROM rt_vehicle_position
                    WHERE ts >= %(since)s AND route_id IS NOT NULL
                    GROUP BY 1, 2
                """, params)

    def rollup_counts(self) -> Dict[str, int]:
        with pg.pool().connection() as conn:
            return {
                "cells": conn.execute(
                    "SELECT COUNT(*) AS n FROM analytics_grid_hour").fetchone()["n"],
                "routes": conn.execute(
                    "SELECT COUNT(*) AS n FROM analytics_route_hour").fetchone()["n"],
            }

    def hourly_series(self, since_ts: int) -> List[Dict[str, Any]]:
        return self._all(
            "SELECT hour_bucket, SUM(observations) AS observations, "
            "SUM(vehicles) AS vehicle_hours, COUNT(DISTINCT route_id) AS routes, "
            "SUM(moving) AS moving, SUM(stopped) AS stopped, "
            "SUM(speed_sum) AS speed_sum, SUM(speed_n) AS speed_n "
            "FROM analytics_route_hour WHERE hour_bucket >= %s "
            "GROUP BY hour_bucket ORDER BY hour_bucket", (since_ts,))

    # ---- quality tier -------------------------------------------------------
    def route_coverage_by_hour(self, since_ts: int) -> List[Dict[str, Any]]:
        return self._all(
            "SELECT hod, weekend, AVG(routes) AS avg_routes_live, COUNT(*) AS samples "
            "FROM ("
            "  SELECT hour_bucket, "
            "         EXTRACT(HOUR FROM to_timestamp(hour_bucket))::int AS hod, "
            "         CASE WHEN EXTRACT(DOW FROM to_timestamp(hour_bucket))::int "
            "              IN (0, 6) THEN 1 ELSE 0 END AS weekend, "
            "         COUNT(DISTINCT route_id) AS routes "
            "  FROM analytics_route_hour WHERE hour_bucket >= %s GROUP BY hour_bucket"
            ") t GROUP BY hod, weekend ORDER BY hod, weekend", (since_ts,))

    def distinct_grid_cells(self, since_ts: int) -> int:
        return self._one(
            "SELECT COUNT(*) AS n FROM "
            "(SELECT DISTINCT cell_y, cell_x FROM analytics_grid_hour "
            "WHERE hour_bucket >= %s) t", (since_ts,))["n"]

    def continuity_report(self, since_ts: int, until_ts: int,
                          gap_threshold_s: int) -> Dict[str, Any]:
        with pg.pool().connection() as conn:
            totals = conn.execute(
                "WITH ordered AS ("
                "  SELECT vehicle_id, "
                "         ts - LAG(ts) OVER (PARTITION BY vehicle_id ORDER BY ts) AS gap "
                "  FROM rt_vehicle_position WHERE ts BETWEEN %(since)s AND %(until)s"
                "), per_vehicle AS ("
                "  SELECT vehicle_id, COUNT(*) AS observations, "
                "         MIN(ts) AS first_ts, MAX(ts) AS last_ts "
                "  FROM rt_vehicle_position WHERE ts BETWEEN %(since)s AND %(until)s "
                "  GROUP BY vehicle_id"
                ") "
                "SELECT (SELECT COUNT(*) FROM per_vehicle) AS vehicles, "
                "(SELECT COALESCE(SUM(observations), 0) FROM per_vehicle) AS observations, "
                "(SELECT COUNT(*) FROM ordered WHERE gap IS NOT NULL) AS gaps_total, "
                "(SELECT COUNT(*) FROM ordered WHERE gap > %(thr)s) AS gaps_over_threshold, "
                "(SELECT AVG(last_ts - first_ts) FROM per_vehicle) AS avg_span_s, "
                "(SELECT AVG(observations::float) FROM per_vehicle) AS avg_observations_per_vehicle",
                {"since": since_ts, "until": until_ts, "thr": gap_threshold_s}).fetchone()
            buckets = conn.execute(
                "WITH ordered AS ("
                "  SELECT ts - LAG(ts) OVER (PARTITION BY vehicle_id ORDER BY ts) AS gap "
                "  FROM rt_vehicle_position WHERE ts BETWEEN %(since)s AND %(until)s"
                ") "
                "SELECT "
                "SUM(CASE WHEN gap > 0 AND gap <= 60 THEN 1 ELSE 0 END) AS b_0_60, "
                "SUM(CASE WHEN gap > 60 AND gap <= 180 THEN 1 ELSE 0 END) AS b_60_180, "
                "SUM(CASE WHEN gap > 180 AND gap <= 300 THEN 1 ELSE 0 END) AS b_180_300, "
                "SUM(CASE WHEN gap > 300 AND gap <= 600 THEN 1 ELSE 0 END) AS b_300_600, "
                "SUM(CASE WHEN gap > 600 THEN 1 ELSE 0 END) AS b_600_plus "
                "FROM ordered WHERE gap IS NOT NULL",
                {"since": since_ts, "until": until_ts}).fetchone()
        out = dict(totals)
        out["gap_buckets"] = dict(buckets)
        return out

    def trip_completeness(self, since_ts: int, until_ts: int,
                          gap_threshold_s: int) -> Dict[str, Any]:
        return self._one(
            "WITH ordered AS ("
            "  SELECT vehicle_id, trip_id, "
            "         ts - LAG(ts) OVER (PARTITION BY vehicle_id, trip_id ORDER BY ts) AS gap "
            "  FROM rt_vehicle_position "
            "  WHERE ts BETWEEN %(since)s AND %(until)s AND trip_id IS NOT NULL"
            "), per_trip AS ("
            "  SELECT vehicle_id, trip_id, "
            "         MAX(CASE WHEN gap > %(thr)s THEN 1 ELSE 0 END) AS has_gap "
            "  FROM ordered GROUP BY vehicle_id, trip_id"
            ") "
            "SELECT COUNT(*) AS trips, COALESCE(SUM(1 - has_gap), 0) AS complete_trips "
            "FROM per_trip",
            {"since": since_ts, "until": until_ts, "thr": gap_threshold_s})

    def referential_integrity(self, since_ts: int) -> Dict[str, Any]:
        return self._one(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN route_id IS NOT NULL AND route_id NOT IN "
            "     (SELECT route_id FROM gtfs_routes) THEN 1 ELSE 0 END) AS invalid_route_id, "
            "SUM(CASE WHEN trip_id IS NOT NULL AND trip_id NOT IN "
            "     (SELECT trip_id FROM gtfs_trips) THEN 1 ELSE 0 END) AS invalid_trip_id "
            "FROM rt_vehicle_position WHERE ts >= %s", (since_ts,))

    def field_population(self, since_ts: int) -> Dict[str, Dict[str, int]]:
        row = dict(self._one(
            "SELECT COUNT(*) AS total, COUNT(stop_id) AS stop_id, "
            "COUNT(occupancy_status) AS occupancy_status, "
            "COUNT(congestion_level) AS congestion_level, "
            "COUNT(current_status) AS current_status, COUNT(bearing) AS bearing, "
            "COUNT(speed) AS speed, COUNT(trip_id) AS trip_id, "
            "COUNT(route_id) AS route_id, COUNT(lat) AS lat "
            "FROM rt_vehicle_position WHERE ts >= %s", (since_ts,)))
        total = row.pop("total")
        return {field: {"populated": n, "total": total} for field, n in row.items()}

    def poll_series(self, since_ts: int) -> List[Dict[str, Any]]:
        return self._all(
            "SELECT polled_at, ok, source, http_status, entity_count, new_rows, "
            "feed_timestamp, latency_ms, error FROM rt_poll_log "
            "WHERE polled_at >= %s ORDER BY polled_at", (since_ts,))


repository: Repository = PostgresRepository()
