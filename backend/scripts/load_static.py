#!/usr/bin/env python3
"""Load the GTFS static .txt feed into the data plane.

    python -m scripts.load_static [--gtfs-dir DIR] [--db PATH] [--force]

Idempotent: re-running replaces the static tables and leaves realtime history
untouched.
"""
import argparse
import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings          # noqa: E402
from app.data import db                  # noqa: E402

BATCH = 20000

# Indexes that make the bulk insert crawl. Dropped for the load, rebuilt after.
DEFERRED_INDEXES = [
    ("ix_stop_times_stop", "CREATE INDEX ix_stop_times_stop ON gtfs_stop_times(stop_id, departure_s)"),
    ("ix_stop_times_dep", "CREATE INDEX ix_stop_times_dep ON gtfs_stop_times(departure_s)"),
    ("ix_trips_route", "CREATE INDEX ix_trips_route ON gtfs_trips(route_id)"),
    ("ix_trips_shape", "CREATE INDEX ix_trips_shape ON gtfs_trips(shape_id)"),
    ("ix_stops_latlon", "CREATE INDEX ix_stops_latlon ON gtfs_stops(stop_lat, stop_lon)"),
]


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def hhmmss_to_seconds(v):
    """GTFS times may exceed 24:00:00 for trips running past midnight."""
    if not v:
        return None
    parts = v.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        return None


def load_table(conn, path, table, columns, transform, label):
    if not path.exists():
        print("  - {0:<16} skipped (no {1})".format(label, path.name))
        return 0
    conn.execute("DELETE FROM " + table)
    sql = ("INSERT OR REPLACE INTO " + table + " (" + ",".join(columns) + ") VALUES ("
           + ",".join("?" * len(columns)) + ")")
    n, batch, started = 0, [], time.time()
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            rec = transform(row)
            if rec is None:
                continue
            batch.append(rec)
            if len(batch) >= BATCH:
                conn.executemany(sql, batch)
                n += len(batch)
                batch = []
    if batch:
        conn.executemany(sql, batch)
        n += len(batch)
    conn.commit()
    print("  - {0:<16} {1:>9,} rows  ({2:.1f}s)".format(label, n, time.time() - started))
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gtfs-dir", type=Path, default=settings.gtfs_static_dir)
    ap.add_argument("--db", type=Path, default=settings.db_path)
    args = ap.parse_args()

    gtfs = args.gtfs_dir
    if not gtfs.exists():
        sys.exit("GTFS directory not found: {0}".format(gtfs))

    print("GTFS static load")
    print("  source: {0}".format(gtfs))
    print("  target: {0}".format(args.db))

    db.init_db(args.db)
    conn = db.connect(args.db)
    started = time.time()
    try:
        for name, _ in DEFERRED_INDEXES:
            conn.execute("DROP INDEX IF EXISTS " + name)
        conn.commit()

        load_table(conn, gtfs / "agency.txt", "gtfs_agency",
                   ["agency_id", "agency_name", "agency_url", "agency_timezone", "agency_lang"],
                   lambda r: (r.get("agency_id"), r.get("agency_name"), r.get("agency_url"),
                              r.get("agency_timezone"), r.get("agency_lang")),
                   "agency")

        load_table(conn, gtfs / "routes.txt", "gtfs_routes",
                   ["route_id", "agency_id", "route_short_name", "route_long_name",
                    "route_desc", "route_type"],
                   lambda r: (r.get("route_id"), r.get("agency_id"), r.get("route_short_name"),
                              r.get("route_long_name"), r.get("route_desc"),
                              to_int(r.get("route_type"))),
                   "routes")

        load_table(conn, gtfs / "stops.txt", "gtfs_stops",
                   ["stop_id", "stop_code", "stop_name", "stop_lat", "stop_lon"],
                   lambda r: (r.get("stop_id"), r.get("stop_code"), r.get("stop_name"),
                              to_float(r.get("stop_lat")), to_float(r.get("stop_lon"))),
                   "stops")

        load_table(conn, gtfs / "trips.txt", "gtfs_trips",
                   ["trip_id", "route_id", "service_id", "trip_headsign",
                    "direction_id", "shape_id"],
                   lambda r: (r.get("trip_id"), r.get("route_id"), r.get("service_id"),
                              r.get("trip_headsign"), to_int(r.get("direction_id")),
                              r.get("shape_id")),
                   "trips")

        load_table(conn, gtfs / "shapes.txt", "gtfs_shapes",
                   ["shape_id", "shape_pt_sequence", "shape_pt_lat", "shape_pt_lon",
                    "shape_dist_traveled"],
                   lambda r: (r.get("shape_id"), to_int(r.get("shape_pt_sequence")),
                              to_float(r.get("shape_pt_lat")), to_float(r.get("shape_pt_lon")),
                              to_float(r.get("shape_dist_traveled"))),
                   "shapes")

        load_table(conn, gtfs / "stop_times.txt", "gtfs_stop_times",
                   ["trip_id", "stop_id", "stop_sequence", "arrival_time",
                    "departure_time", "arrival_s", "departure_s"],
                   lambda r: (r.get("trip_id"), r.get("stop_id"), to_int(r.get("stop_sequence")),
                              r.get("arrival_time"), r.get("departure_time"),
                              hhmmss_to_seconds(r.get("arrival_time")),
                              hhmmss_to_seconds(r.get("departure_time"))),
                   "stop_times")

        print("  rebuilding indexes ...")
        for _, ddl in DEFERRED_INDEXES:
            conn.execute(ddl)
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('static_loaded_at', ?)",
                     (str(int(time.time())),))
        conn.commit()
        print("  analyzing ...")
        conn.execute("ANALYZE")
        conn.commit()
    finally:
        conn.close()

    size_mb = args.db.stat().st_size / 1e6
    print("Done in {0:.1f}s. Database is {1:.0f} MB.".format(time.time() - started, size_mb))


if __name__ == "__main__":
    main()
