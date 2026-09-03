"""SQLite implementation of the data-plane port."""
import time
from typing import Any, Dict, List, Optional, Sequence

from . import db
from .repository import Repository

_VEHICLE_COLS = (
    "vehicle_id, ts, trip_id, route_id, lat, lon, bearing, speed, stop_id, "
    "current_status, congestion_level, occupancy_status, ingested_at"
)


def _rows(cur) -> List[Dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]


class SqliteRepository(Repository):
    # ---- static -----------------------------------------------------------
    def feed_summary(self) -> Dict[str, Any]:
        c = db.get_connection()
        out: Dict[str, Any] = {}
        for table, key in (
            ("gtfs_agency", "agencies"), ("gtfs_routes", "routes"),
            ("gtfs_stops", "stops"), ("gtfs_trips", "trips"),
            ("gtfs_stop_times", "stop_times"), ("gtfs_shapes", "shape_points"),
        ):
            out[key] = c.execute("SELECT COUNT(*) AS n FROM " + table).fetchone()["n"]
        bbox = c.execute(
            "SELECT MIN(stop_lat) AS min_lat, MIN(stop_lon) AS min_lon, "
            "MAX(stop_lat) AS max_lat, MAX(stop_lon) AS max_lon FROM gtfs_stops"
        ).fetchone()
        out["bbox"] = dict(bbox) if bbox and bbox["min_lat"] is not None else None
        loaded = c.execute("SELECT value FROM meta WHERE key='static_loaded_at'").fetchone()
        out["static_loaded_at"] = int(loaded["value"]) if loaded else None
        out["history_rows"] = c.execute(
            "SELECT COUNT(*) AS n FROM rt_vehicle_position").fetchone()["n"]
        return out

    def list_routes(self, q: Optional[str], limit: int, offset: int) -> Dict[str, Any]:
        c = db.get_connection()
        where, params = "", []
        if q:
            where = ("WHERE route_short_name LIKE ? OR route_long_name LIKE ? "
                     "OR route_desc LIKE ?")
            params = ["%" + q + "%"] * 3
        total = c.execute(
            "SELECT COUNT(*) AS n FROM gtfs_routes " + where, params).fetchone()["n"]
        items = _rows(c.execute(
            "SELECT r.*, (SELECT COUNT(*) FROM gtfs_trips t WHERE t.route_id=r.route_id) "
            "AS trip_count FROM gtfs_routes r " + where +
            " ORDER BY route_short_name LIMIT ? OFFSET ?", params + [limit, offset]))
        return {"total": total, "items": items}

    def get_route(self, route_id: str) -> Optional[Dict[str, Any]]:
        c = db.get_connection()
        row = c.execute(
            "SELECT r.*, (SELECT COUNT(*) FROM gtfs_trips t WHERE t.route_id=r.route_id) "
            "AS trip_count FROM gtfs_routes r WHERE r.route_id=?", (route_id,)).fetchone()
        return dict(row) if row else None

    def route_shape(self, route_id: str) -> List[List[float]]:
        c = db.get_connection()
        # Pick the shape with the most points: for a route with variants that is
        # the fullest representation of the corridor.
        shape = c.execute(
            "SELECT s.shape_id FROM gtfs_shapes s "
            "WHERE s.shape_id IN (SELECT DISTINCT shape_id FROM gtfs_trips WHERE route_id=?) "
            "GROUP BY s.shape_id ORDER BY COUNT(*) DESC LIMIT 1", (route_id,)).fetchone()
        if not shape:
            return []
        pts = c.execute(
            "SELECT shape_pt_lon, shape_pt_lat FROM gtfs_shapes WHERE shape_id=? "
            "ORDER BY shape_pt_sequence", (shape["shape_id"],)).fetchall()
        return [[p["shape_pt_lon"], p["shape_pt_lat"]] for p in pts]

    def route_stops(self, route_id: str) -> List[Dict[str, Any]]:
        c = db.get_connection()
        # Stops of the representative (longest) trip on the route.
        trip = c.execute(
            "SELECT st.trip_id FROM gtfs_stop_times st "
            "JOIN gtfs_trips t ON t.trip_id = st.trip_id WHERE t.route_id=? "
            "GROUP BY st.trip_id ORDER BY COUNT(*) DESC LIMIT 1", (route_id,)).fetchone()
        if not trip:
            return []
        return _rows(c.execute(
            "SELECT s.stop_id, s.stop_name, s.stop_lat, s.stop_lon, st.stop_sequence, "
            "st.arrival_time, st.departure_time FROM gtfs_stop_times st "
            "JOIN gtfs_stops s ON s.stop_id = st.stop_id WHERE st.trip_id=? "
            "ORDER BY st.stop_sequence", (trip["trip_id"],)))

    def list_stops(self, q: Optional[str], bbox: Optional[Sequence[float]],
                   limit: int) -> List[Dict[str, Any]]:
        c = db.get_connection()
        clauses, params = [], []
        if q:
            clauses.append("(stop_name LIKE ? OR stop_code LIKE ?)")
            params += ["%" + q + "%"] * 2
        if bbox:
            clauses.append("stop_lon BETWEEN ? AND ? AND stop_lat BETWEEN ? AND ?")
            params += [bbox[0], bbox[2], bbox[1], bbox[3]]
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return _rows(c.execute(
            "SELECT stop_id, stop_code, stop_name, stop_lat, stop_lon FROM gtfs_stops "
            + where + " LIMIT ?", params + [limit]))

    def stop_schedule(self, stop_id: str, from_s: int, to_s: int,
                      limit: int) -> List[Dict[str, Any]]:
        c = db.get_connection()
        return _rows(c.execute(
            "SELECT st.trip_id, st.arrival_time, st.departure_time, st.departure_s, "
            "t.route_id, r.route_short_name, r.route_desc "
            "FROM gtfs_stop_times st "
            "JOIN gtfs_trips t  ON t.trip_id = st.trip_id "
            "LEFT JOIN gtfs_routes r ON r.route_id = t.route_id "
            "WHERE st.stop_id=? AND st.departure_s BETWEEN ? AND ? "
            "ORDER BY st.departure_s LIMIT ?", (stop_id, from_s, to_s, limit)))

    def route_names(self, route_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        ids = [r for r in route_ids if r]
        if not ids:
            return {}
        c = db.get_connection()
        out: Dict[str, Dict[str, Any]] = {}
        for i in range(0, len(ids), 500):        # stay under SQLITE_MAX_VARIABLE_NUMBER
            chunk = ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for r in c.execute(
                "SELECT route_id, route_short_name, route_long_name, route_desc, agency_id "
                "FROM gtfs_routes WHERE route_id IN (" + marks + ")", chunk):
                out[r["route_id"]] = dict(r)
        return out

    # ---- realtime ---------------------------------------------------------
    def upsert_vehicles(self, rows: List[Dict[str, Any]]) -> int:
        if not rows:
            return 0
        c = db.get_connection()
        values = [
            (r["vehicle_id"], r["ts"], r.get("trip_id"), r.get("route_id"),
             r.get("lat"), r.get("lon"), r.get("bearing"), r.get("speed"),
             r.get("stop_id"), r.get("current_status"), r.get("congestion_level"),
             r.get("occupancy_status"), r["ingested_at"])
            for r in rows
        ]
        marks = "(" + ",".join("?" * 13) + ")"
        before = c.total_changes
        c.executemany(
            "INSERT OR IGNORE INTO rt_vehicle_position (" + _VEHICLE_COLS + ") VALUES " + marks,
            values)
        inserted = c.total_changes - before
        # Latest wins only if the observation is not older than what we hold.
        c.executemany(
            "INSERT INTO rt_vehicle_latest (" + _VEHICLE_COLS + ") VALUES " + marks +
            " ON CONFLICT(vehicle_id) DO UPDATE SET "
            "ts=excluded.ts, trip_id=excluded.trip_id, route_id=excluded.route_id, "
            "lat=excluded.lat, lon=excluded.lon, bearing=excluded.bearing, "
            "speed=excluded.speed, stop_id=excluded.stop_id, "
            "current_status=excluded.current_status, "
            "congestion_level=excluded.congestion_level, "
            "occupancy_status=excluded.occupancy_status, "
            "ingested_at=excluded.ingested_at "
            "WHERE excluded.ts >= rt_vehicle_latest.ts",
            values)
        c.commit()
        return inserted

    def latest_vehicles(self, route_id: Optional[str],
                        bbox: Optional[Sequence[float]],
                        max_age_s: Optional[int]) -> List[Dict[str, Any]]:
        c = db.get_connection()
        clauses, params = [], []
        if route_id:
            clauses.append("route_id = ?")
            params.append(route_id)
        if bbox:
            clauses.append("lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?")
            params += [bbox[0], bbox[2], bbox[1], bbox[3]]
        if max_age_s:
            clauses.append("ts >= ?")
            params.append(int(time.time()) - max_age_s)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return _rows(c.execute(
            "SELECT " + _VEHICLE_COLS + " FROM rt_vehicle_latest " + where, params))

    def vehicle_history(self, vehicle_id: str, since_ts: int) -> List[Dict[str, Any]]:
        c = db.get_connection()
        return _rows(c.execute(
            "SELECT " + _VEHICLE_COLS + " FROM rt_vehicle_position "
            "WHERE vehicle_id=? AND ts >= ? ORDER BY ts", (vehicle_id, since_ts)))

    def log_poll(self, **kw: Any) -> None:
        c = db.get_connection()
        c.execute(
            "INSERT INTO rt_poll_log (polled_at, ok, source, http_status, entity_count, "
            "new_rows, feed_timestamp, latency_ms, error) VALUES (?,?,?,?,?,?,?,?,?)",
            (kw.get("polled_at", int(time.time())), 1 if kw.get("ok") else 0,
             kw.get("source", "feed"), kw.get("http_status"), kw.get("entity_count"),
             kw.get("new_rows"), kw.get("feed_timestamp"), kw.get("latency_ms"),
             kw.get("error")))
        c.commit()

    def recent_polls(self, limit: int) -> List[Dict[str, Any]]:
        c = db.get_connection()
        return _rows(c.execute(
            "SELECT * FROM rt_poll_log ORDER BY polled_at DESC LIMIT ?", (limit,)))

    def prune_history(self, older_than_ts: int) -> int:
        c = db.get_connection()
        cur = c.execute("DELETE FROM rt_vehicle_position WHERE ts < ?", (older_than_ts,))
        c.execute("DELETE FROM rt_poll_log WHERE polled_at < ?", (older_than_ts,))
        c.commit()
        return cur.rowcount

    def purge_simulated(self) -> int:
        c = db.get_connection()
        cur = c.execute("DELETE FROM rt_vehicle_latest WHERE vehicle_id LIKE 'SIM-%'")
        c.execute("DELETE FROM rt_vehicle_position WHERE vehicle_id LIKE 'SIM-%'")
        c.commit()
        return cur.rowcount

    def route_count(self) -> int:
        return db.get_connection().execute(
            "SELECT COUNT(*) AS n FROM gtfs_routes").fetchone()["n"]

    def dark_routes(self, limit: int) -> List[Dict[str, Any]]:
        c = db.get_connection()
        return _rows(c.execute(
            "SELECT r.route_id, r.route_short_name, r.route_desc, "
            "(SELECT COUNT(*) FROM gtfs_trips t WHERE t.route_id = r.route_id) AS trip_count "
            "FROM gtfs_routes r "
            "WHERE r.route_id NOT IN (SELECT DISTINCT route_id FROM rt_vehicle_latest "
            "                         WHERE route_id IS NOT NULL) "
            "ORDER BY trip_count DESC LIMIT ?", (limit,)))

    # ---- analytics --------------------------------------------------------
    def fleet_stats(self, window_s: int) -> Dict[str, Any]:
        c = db.get_connection()
        cutoff = int(time.time()) - window_s
        row = c.execute(
            "SELECT COUNT(*) AS active_vehicles, "
            "COUNT(DISTINCT route_id) AS active_routes, "
            "AVG(speed) AS avg_speed, MAX(ts) AS newest_ts, MIN(ts) AS oldest_ts "
            "FROM rt_vehicle_latest WHERE ts >= ?", (cutoff,)).fetchone()
        stats = dict(row) if row else {}
        stats["moving"] = c.execute(
            "SELECT COUNT(*) AS n FROM rt_vehicle_latest WHERE ts >= ? AND speed > 0.5",
            (cutoff,)).fetchone()["n"]
        stats["stale_vehicles"] = c.execute(
            "SELECT COUNT(*) AS n FROM rt_vehicle_latest WHERE ts < ?",
            (cutoff,)).fetchone()["n"]
        stats["window_s"] = window_s
        return stats

    def active_routes(self, limit: int) -> List[Dict[str, Any]]:
        c = db.get_connection()
        return _rows(c.execute(
            "SELECT v.route_id, COUNT(*) AS vehicles, AVG(v.speed) AS avg_speed, "
            "r.route_short_name, r.route_desc "
            "FROM rt_vehicle_latest v LEFT JOIN gtfs_routes r ON r.route_id = v.route_id "
            "WHERE v.route_id IS NOT NULL "
            "GROUP BY v.route_id ORDER BY vehicles DESC LIMIT ?", (limit,)))

    def ingest_timeseries(self, bucket_s: int, since_ts: int) -> List[Dict[str, Any]]:
        c = db.get_connection()
        return _rows(c.execute(
            "SELECT (ts / ?) * ? AS bucket, COUNT(*) AS observations, "
            "COUNT(DISTINCT vehicle_id) AS vehicles, AVG(speed) AS avg_speed "
            "FROM rt_vehicle_position WHERE ts >= ? "
            "GROUP BY bucket ORDER BY bucket", (bucket_s, bucket_s, since_ts)))

    def speed_histogram(self, since_ts: int, buckets: int) -> List[Dict[str, Any]]:
        c = db.get_connection()
        # 2 m/s wide bins (~7.2 km/h), clamped into the top bin.
        return _rows(c.execute(
            "SELECT MIN(CAST(speed / 2 AS INTEGER), ?) AS bin, COUNT(*) AS n "
            "FROM rt_vehicle_position WHERE ts >= ? AND speed IS NOT NULL "
            "GROUP BY bin ORDER BY bin", (buckets - 1, since_ts)))


    # ---- analytics tier (rollups) -----------------------------------------
    @staticmethod
    def _grid_bounds(bbox, lat_deg, lon_deg):
        """bbox -> cell-index bounds, so the primary key stays usable instead
        of filtering on a derived coordinate."""
        return (int(bbox[1] / lat_deg) - 1, int(bbox[3] / lat_deg) + 1,
                int(bbox[0] / lon_deg) - 1, int(bbox[2] / lon_deg) + 1)

    def grid_cells(self, since_ts: int, factor: int,
                   bbox: Optional[Sequence[float]], min_observations: int,
                   limit: int) -> List[Dict[str, Any]]:
        from ..services.aggregator import GRID_LAT_DEG, GRID_LON_DEG
        clauses, params = ["hour_bucket >= ?"], [since_ts]
        if bbox:
            y0, y1, x0, x1 = self._grid_bounds(bbox, GRID_LAT_DEG, GRID_LON_DEG)
            clauses.append("cell_y BETWEEN ? AND ? AND cell_x BETWEEN ? AND ?")
            params += [y0, y1, x0, x1]
        where = " AND ".join(clauses)
        c = db.get_connection()
        return _rows(c.execute(
            "SELECT cell_y / ? AS gy, cell_x / ? AS gx, "
            "SUM(observations) AS observations, MAX(vehicles) AS peak_vehicles, "
            "SUM(moving) AS moving, SUM(stopped) AS stopped, "
            "SUM(speed_sum) AS speed_sum, SUM(speed_n) AS speed_n, "
            "MIN(speed_min) AS speed_min, MAX(speed_max) AS speed_max "
            "FROM analytics_grid_hour WHERE " + where +
            " GROUP BY gy, gx HAVING SUM(observations) >= ? "
            "ORDER BY observations DESC LIMIT ?",
            [factor, factor] + params + [min_observations, limit]))

    def grid_extent(self, since_ts: int) -> Dict[str, Any]:
        c = db.get_connection()
        row = c.execute(
            "SELECT MAX(observations) AS max_obs, "
            "MIN(CASE WHEN speed_n > 0 THEN speed_sum / speed_n END) AS min_speed, "
            "MAX(CASE WHEN speed_n > 0 THEN speed_sum / speed_n END) AS max_speed, "
            "COUNT(*) AS cell_hours, MIN(hour_bucket) AS first_hour, "
            "MAX(hour_bucket) AS last_hour "
            "FROM analytics_grid_hour WHERE hour_bucket >= ?", (since_ts,)).fetchone()
        return dict(row) if row else {}

    def nearest_stop(self, lat: float, lon: float,
                     radius_deg: float) -> Optional[Dict[str, Any]]:
        c = db.get_connection()
        row = c.execute(
            "SELECT stop_id, stop_name, stop_lat, stop_lon, "
            "((stop_lat - ?) * (stop_lat - ?) + (stop_lon - ?) * (stop_lon - ?)) AS d2 "
            "FROM gtfs_stops WHERE stop_lat BETWEEN ? AND ? AND stop_lon BETWEEN ? AND ? "
            "ORDER BY d2 LIMIT 1",
            (lat, lat, lon, lon, lat - radius_deg, lat + radius_deg,
             lon - radius_deg, lon + radius_deg)).fetchone()
        return dict(row) if row else None

    def route_corridor(self, route_id: str, since_ts: int) -> List[Dict[str, Any]]:
        # One route holds few observations, so this reads the log directly
        # rather than widening the grid rollup with a route dimension.
        from ..services.aggregator import (GRID_LAT_DEG, GRID_LON_DEG,
                                           IMPLAUSIBLE_MPS, MOVING_MPS)
        c = db.get_connection()
        return _rows(c.execute(
            "SELECT CAST(lat / ? AS INTEGER) AS gy, CAST(lon / ? AS INTEGER) AS gx, "
            "COUNT(*) AS observations, AVG(lat) AS lat, AVG(lon) AS lon, "
            "SUM(CASE WHEN speed > ? AND speed < ? THEN speed ELSE 0 END) AS speed_sum, "
            "SUM(CASE WHEN speed > ? AND speed < ? THEN 1 ELSE 0 END) AS speed_n, "
            "SUM(CASE WHEN speed <= ? THEN 1 ELSE 0 END) AS stopped "
            "FROM rt_vehicle_position "
            "WHERE route_id = ? AND ts >= ? AND lat IS NOT NULL "
            "GROUP BY gy, gx ORDER BY observations DESC",
            (GRID_LAT_DEG, GRID_LON_DEG, MOVING_MPS, IMPLAUSIBLE_MPS,
             MOVING_MPS, IMPLAUSIBLE_MPS, MOVING_MPS, route_id, since_ts)))

    def hourly_series(self, since_ts: int) -> List[Dict[str, Any]]:
        c = db.get_connection()
        return _rows(c.execute(
            "SELECT hour_bucket, SUM(observations) AS observations, "
            "SUM(vehicles) AS vehicle_hours, COUNT(DISTINCT route_id) AS routes, "
            "SUM(moving) AS moving, SUM(stopped) AS stopped, "
            "SUM(speed_sum) AS speed_sum, SUM(speed_n) AS speed_n "
            "FROM analytics_route_hour WHERE hour_bucket >= ? "
            "GROUP BY hour_bucket ORDER BY hour_bucket", (since_ts,)))


repository: Repository = SqliteRepository()
