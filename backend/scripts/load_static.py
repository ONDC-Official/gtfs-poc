#!/usr/bin/env python3
"""Load the GTFS static .txt feed into the data plane.

    python -m scripts.load_static [--gtfs-dir DIR] [--db PATH] [--backend sqlite|postgres]

Idempotent: re-running replaces the static tables (gtfs_*) and leaves the
realtime history untouched. The backend defaults to DB_BACKEND; for postgres
the target is DATABASE_URL and --db is ignored.
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

# Indexes that make the SQLite bulk insert crawl. Dropped for the load, rebuilt
# after. Postgres keeps its indexes - COPY into an indexed table is fine there.
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


# One place both backends read: (feed file, table, columns, row -> tuple, label).
# Column lists deliberately exclude the generated `geom` columns on the
# Postgres side - they populate themselves from lat/lon.
TABLES = [
    ("agency.txt", "gtfs_agency",
     ["agency_id", "agency_name", "agency_url", "agency_timezone", "agency_lang"],
     lambda r: (r.get("agency_id"), r.get("agency_name"), r.get("agency_url"),
                r.get("agency_timezone"), r.get("agency_lang")),
     "agency"),
    ("routes.txt", "gtfs_routes",
     ["route_id", "agency_id", "route_short_name", "route_long_name",
      "route_desc", "route_type"],
     lambda r: (r.get("route_id"), r.get("agency_id"), r.get("route_short_name"),
                r.get("route_long_name"), r.get("route_desc"),
                to_int(r.get("route_type"))),
     "routes"),
    ("stops.txt", "gtfs_stops",
     ["stop_id", "stop_code", "stop_name", "stop_lat", "stop_lon"],
     lambda r: (r.get("stop_id"), r.get("stop_code"), r.get("stop_name"),
                to_float(r.get("stop_lat")), to_float(r.get("stop_lon"))),
     "stops"),
    ("trips.txt", "gtfs_trips",
     ["trip_id", "route_id", "service_id", "trip_headsign",
      "direction_id", "shape_id"],
     lambda r: (r.get("trip_id"), r.get("route_id"), r.get("service_id"),
                r.get("trip_headsign"), to_int(r.get("direction_id")),
                r.get("shape_id")),
     "trips"),
    ("shapes.txt", "gtfs_shapes",
     ["shape_id", "shape_pt_sequence", "shape_pt_lat", "shape_pt_lon",
      "shape_dist_traveled"],
     lambda r: (r.get("shape_id"), to_int(r.get("shape_pt_sequence")),
                to_float(r.get("shape_pt_lat")), to_float(r.get("shape_pt_lon")),
                to_float(r.get("shape_dist_traveled"))),
     "shapes"),
    ("stop_times.txt", "gtfs_stop_times",
     ["trip_id", "stop_id", "stop_sequence", "arrival_time",
      "departure_time", "arrival_s", "departure_s"],
     lambda r: (r.get("trip_id"), r.get("stop_id"), to_int(r.get("stop_sequence")),
                r.get("arrival_time"), r.get("departure_time"),
                hhmmss_to_seconds(r.get("arrival_time")),
                hhmmss_to_seconds(r.get("departure_time"))),
     "stop_times"),
]


def _rows(path: Path, transform):
    """Transformed record tuples from a GTFS .txt file, skipping blanks."""
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            rec = transform(row)
            if rec is not None:
                yield rec


# --------------------------------------------------------------------------
# SQLite
# --------------------------------------------------------------------------
def load_table_sqlite(conn, path, table, columns, transform, label):
    if not path.exists():
        print("  - {0:<16} skipped (no {1})".format(label, path.name))
        return 0
    conn.execute("DELETE FROM " + table)
    sql = ("INSERT OR REPLACE INTO " + table + " (" + ",".join(columns) + ") VALUES ("
           + ",".join("?" * len(columns)) + ")")
    n, batch, started = 0, [], time.time()
    for rec in _rows(path, transform):
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


def main_sqlite(gtfs: Path, db_path: Path):
    print("  target: {0}  (sqlite)".format(db_path))
    db.init_db(db_path)
    conn = db.connect(db_path)
    started = time.time()
    try:
        for name, _ in DEFERRED_INDEXES:
            conn.execute("DROP INDEX IF EXISTS " + name)
        conn.commit()
        for fname, table, columns, transform, label in TABLES:
            load_table_sqlite(conn, gtfs / fname, table, columns, transform, label)
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
    size_mb = db_path.stat().st_size / 1e6
    print("Done in {0:.1f}s. Database is {1:.0f} MB.".format(time.time() - started, size_mb))


# --------------------------------------------------------------------------
# PostgreSQL + PostGIS
# --------------------------------------------------------------------------
def load_table_postgres(conn, path, table, columns, transform, label):
    if not path.exists():
        print("  - {0:<16} skipped (no {1})".format(label, path.name))
        return 0
    started = time.time()
    collist = ",".join(columns)
    with conn.transaction():
        # Stage into a constraint-free copy of just the plain columns (no PK,
        # no geom), COPY the file in, then move it across with ON CONFLICT so a
        # duplicate primary key in the feed is dropped rather than aborting -
        # the SQLite path gets the same effect from INSERT OR REPLACE.
        conn.execute("TRUNCATE " + table)
        conn.execute("CREATE TEMP TABLE _stg AS SELECT " + collist +
                     " FROM " + table + " WITH NO DATA")
        with conn.cursor() as cur:
            with cur.copy("COPY _stg (" + collist + ") FROM STDIN") as copy:
                n = 0
                for rec in _rows(path, transform):
                    copy.write_row(rec)
                    n += 1
        conn.execute("INSERT INTO " + table + " (" + collist + ") "
                     "SELECT " + collist + " FROM _stg ON CONFLICT DO NOTHING")
        conn.execute("DROP TABLE _stg")
    print("  - {0:<16} {1:>9,} rows  ({2:.1f}s)".format(label, n, time.time() - started))
    return n


def main_postgres(gtfs: Path):
    import psycopg
    from app.data import pg

    print("  target: {0}  (postgres)".format(settings.database_url))
    pg.init_db()                                  # apply schema_postgres.sql
    started = time.time()
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        for fname, table, columns, transform, label in TABLES:
            load_table_postgres(conn, gtfs / fname, table, columns, transform, label)
        conn.execute("INSERT INTO meta (key, value) VALUES ('static_loaded_at', %s) "
                     "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                     (str(int(time.time())),))
        print("  analyzing ...")
        conn.execute("ANALYZE")
    print("Done in {0:.1f}s.".format(time.time() - started))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gtfs-dir", type=Path, default=settings.gtfs_static_dir)
    ap.add_argument("--db", type=Path, default=settings.db_path)
    ap.add_argument("--backend", choices=["sqlite", "postgres"],
                    default=settings.db_backend)
    args = ap.parse_args()

    gtfs = args.gtfs_dir
    if not gtfs.exists():
        sys.exit("GTFS directory not found: {0}".format(gtfs))

    print("GTFS static load")
    print("  source: {0}".format(gtfs))
    if args.backend == "postgres":
        main_postgres(gtfs)
    else:
        main_sqlite(gtfs, args.db)


if __name__ == "__main__":
    main()
