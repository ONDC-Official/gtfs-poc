"""Fixtures for the Repository contract suite.

One dataset, one set of assertions, run against every adapter. Adding the
Postgres adapter means appending to ADAPTERS - no test changes.

The dataset is deliberately small and hand-checkable: every assertion in
test_repository_contract.py is derived from the literals below, so a failure
points at the adapter rather than at a fixture that drifted.
"""
import math
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Settings is a module-level singleton built at import, so the database has to
# be redirected before anything under app/ is imported.
_TMP = Path(tempfile.mkdtemp(prefix="gtfs-contract-"))
os.environ["DB_PATH"] = str(_TMP / "contract.db")
# Never let a stray key start the poller during a test run.
os.environ["RT_API_KEY"] = ""
os.environ["RT_MOCK_WHEN_UNCONFIGURED"] = "false"

# Where the Postgres adapter's pool connects, if the suite reaches that far.
# Set before the settings singleton is built; harmless when only SQLite runs.
_PG_DSN = os.environ.get(
    "GTFS_TEST_PG_DSN", "postgresql://postgres:test@localhost:55432/gtfs")
os.environ["DATABASE_URL"] = _PG_DSN

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.data import db                                    # noqa: E402
from app.data.sqlite_repo import SqliteRepository          # noqa: E402
from app.services.aggregator import GRID_LAT_DEG, GRID_LON_DEG  # noqa: E402

# ---------------------------------------------------------------------------
# Fixed clock. Every ts below is an offset from this, so assertions about
# freshness and pruning are exact rather than racing the wall clock.
NOW = 1_700_000_000
HOUR = 3600

# Coordinates whose grid quotient has a fractional part of 0.7 - far enough
# from .5 to be unambiguous. SQLite's CAST truncates to N; Postgres' CAST
# rounds to N+1. route_corridor() must land on N (floor) on both, so this
# probe is what catches that divergence during the port.
CELL_Y, CELL_X = 12_739, 30_188
PROBE_LAT = (CELL_Y + 0.7) * GRID_LAT_DEG
PROBE_LON = (CELL_X + 0.7) * GRID_LON_DEG

# A stop cluster with hand-computed separations (see test_routes_nearby).
HUB_LAT, HUB_LON = 28.6200, 77.2160
M_PER_DEG = 111_320.0
COSLAT = math.cos(math.radians(HUB_LAT))

DATASET = {
    "gtfs_agency": (
        ("agency_id", "agency_name", "agency_timezone"),
        [("DTC", "Delhi Transport Corporation", "Asia/Kolkata"),
         ("DOT", "Delhi Integrated Multi-Modal Transit", "Asia/Kolkata")],
    ),
    # route_desc on R1 is mixed-case on purpose: the search tests query it in
    # lower case, which SQLite's LIKE matches and Postgres' LIKE does not.
    "gtfs_routes": (
        ("route_id", "agency_id", "route_short_name", "route_long_name",
         "route_desc", "route_type"),
        [("R1", "DTC", "764", "Kashmere Gate - Connaught Place", "Via RING Road", 3),
         ("R2", "DOT", "505", "Janpath Loop", "Via Janpath", 3),
         ("R3", "DTC", "AC-9", "Airport Express", "Via NH8", 3),
         ("R4", "DTC", "999", "Dark Route", "Never reports", 3)],
    ),
    "gtfs_stops": (
        ("stop_id", "stop_code", "stop_name", "stop_lat", "stop_lon"),
        [("S1", "C001", "Connaught Place", HUB_LAT, HUB_LON),
         ("S2", "C002", "Barakhamba Road", HUB_LAT + 0.001, HUB_LON),
         ("S3", "C003", "Janpath", HUB_LAT, HUB_LON + 0.001),
         ("S4", "C004", "Far Depot", HUB_LAT + 0.02, HUB_LON),
         ("S5", "C005", "Probe Corner", PROBE_LAT, PROBE_LON)],
    ),
    # T1 has 3 stops, T2 has 2 - route_stops() must pick T1 as representative.
    "gtfs_trips": (
        ("trip_id", "route_id", "service_id", "trip_headsign", "direction_id", "shape_id"),
        [("T1", "R1", "WD", "To CP", 0, "SH1"),
         ("T2", "R1", "WD", "To Kashmere Gate", 1, "SH2"),
         ("T3", "R2", "WD", "Janpath Loop", 0, "SH2"),
         ("T4", "R4", "WD", "Dark", 0, None)],
    ),
    "gtfs_stop_times": (
        ("trip_id", "stop_id", "stop_sequence", "arrival_time", "departure_time",
         "arrival_s", "departure_s"),
        [("T1", "S1", 1, "08:00:00", "08:00:30", 28800, 28830),
         ("T1", "S2", 2, "08:10:00", "08:10:30", 29400, 29430),
         ("T1", "S3", 3, "08:20:00", "08:20:30", 30000, 30030),
         ("T2", "S1", 1, "09:00:00", "09:00:30", 32400, 32430),
         ("T2", "S2", 2, "09:10:00", "09:10:30", 33000, 33030),
         ("T3", "S3", 1, "10:00:00", "10:00:30", 36000, 36030),
         # Past midnight: GTFS allows >24h and the loader keeps it as seconds.
         ("T4", "S4", 1, "25:00:00", "25:00:30", 90000, 90030)],
    ),
    # SH1 has more points than SH2, so route_shape() must return SH1 for R1.
    "gtfs_shapes": (
        ("shape_id", "shape_pt_sequence", "shape_pt_lat", "shape_pt_lon", "shape_dist_traveled"),
        [("SH1", i, HUB_LAT + i * 0.001, HUB_LON + i * 0.001, i * 100.0) for i in range(5)]
        + [("SH2", i, HUB_LAT - i * 0.001, HUB_LON, i * 100.0) for i in range(3)],
    ),
}

# vehicle_id, ts, trip_id, route_id, lat, lon, bearing, speed
#   V1 - four observations on R1, one hour apart, moving.
#   V2 - two observations on R2; the second is STOPPED (speed below 0.5).
#   V3 - stale (well outside any freshness window) and has a null position.
#   SIM-1 - mock leftover, must be purged by purge_simulated().
OBSERVATIONS = [
    ("V1", NOW - 3 * HOUR, "T1", "R1", PROBE_LAT, PROBE_LON, 90.0, 5.0),
    ("V1", NOW - 2 * HOUR, "T1", "R1", PROBE_LAT, PROBE_LON, 90.0, 11.0),
    ("V1", NOW - 1 * HOUR, "T1", "R1", HUB_LAT, HUB_LON, 90.0, 7.0),
    ("V1", NOW - 60,       "T1", "R1", HUB_LAT, HUB_LON, 90.0, 9.0),
    ("V2", NOW - 2 * HOUR, "T3", "R2", HUB_LAT, HUB_LON, 180.0, 3.0),
    ("V2", NOW - 90,       "T3", "R2", HUB_LAT, HUB_LON, 180.0, 0.0),
    ("V3", NOW - 40000,    None, "R3", None,     None,    None,  None),
    ("SIM-1", NOW - 120,   None, "R1", HUB_LAT, HUB_LON, 0.0,   6.0),
]

_VEHICLE_COLS = ("vehicle_id", "ts", "trip_id", "route_id", "lat", "lon",
                 "bearing", "speed", "ingested_at")

POLLS = [
    (NOW - 300, 1, "feed", 200, 5000, 4800, NOW - 305, 420, None),
    (NOW - 270, 1, "feed", 200, 5010, 4900, NOW - 275, 380, None),
    (NOW - 240, 0, "feed", 503, 0, 0, None, 20000, "HTTP 503"),
]
_POLL_COLS = ("polled_at", "ok", "source", "http_status", "entity_count",
              "new_rows", "feed_timestamp", "latency_ms", "error")


def _insert(conn, table, cols, rows, ph):
    if not rows:
        return
    marks = "(" + ",".join([ph] * len(cols)) + ")"
    sql = "INSERT INTO {0} ({1}) VALUES {2}".format(table, ",".join(cols), marks)
    if hasattr(conn, "executemany"):        # sqlite3.Connection
        conn.executemany(sql, rows)
    else:                                   # psycopg.Connection - cursor only
        with conn.cursor() as cur:
            cur.executemany(sql, rows)


def _seed_sqlite():
    """Wipe and re-seed. Function-scoped, so tests never see each other's writes."""
    conn = db.get_connection()
    for table in ("gtfs_agency", "gtfs_routes", "gtfs_stops", "gtfs_trips",
                  "gtfs_stop_times", "gtfs_shapes", "rt_vehicle_position",
                  "rt_vehicle_latest", "rt_poll_log", "meta",
                  "analytics_grid_hour", "analytics_route_hour"):
        conn.execute("DELETE FROM " + table)

    for table, (cols, rows) in DATASET.items():
        _insert(conn, table, cols, rows, "?")

    obs = [r + (NOW,) for r in OBSERVATIONS]
    _insert(conn, "rt_vehicle_position", _VEHICLE_COLS, obs, "?")
    # rt_vehicle_latest is the newest observation per vehicle.
    latest = {}
    for r in obs:
        if r[0] not in latest or r[1] > latest[r[0]][1]:
            latest[r[0]] = r
    _insert(conn, "rt_vehicle_latest", _VEHICLE_COLS, list(latest.values()), "?")
    _insert(conn, "rt_poll_log", _POLL_COLS, POLLS, "?")
    conn.execute("INSERT INTO meta (key, value) VALUES ('static_loaded_at', ?)",
                 (str(NOW - 86400),))
    conn.commit()


ADAPTERS = [("sqlite", SqliteRepository, _seed_sqlite)]

# ---------------------------------------------------------------------------
# Postgres adapter - added to ADAPTERS only when a PostGIS database is
# reachable, so `pytest` still runs anywhere. Point it with GTFS_TEST_PG_DSN
# (read at the top of this file); the default matches the throwaway container
# used during development:
#     docker run -d --name gtfs-pg -e POSTGRES_PASSWORD=test \
#         -e POSTGRES_DB=gtfs -p 55432:5432 postgis/postgis:16-3.4
try:
    import psycopg
    with psycopg.connect(_PG_DSN, connect_timeout=3) as _probe:
        _probe.execute("SELECT 1")
    _PG_OK = True
except Exception:
    _PG_OK = False

if _PG_OK:
    from app.data import pg                               # noqa: E402
    from app.data.postgres_repo import PostgresRepository  # noqa: E402

    pg.init_db()   # once - the script is IF-NOT-EXISTS throughout

    _PG_TABLES = ("gtfs_agency", "gtfs_routes", "gtfs_stops", "gtfs_trips",
                  "gtfs_stop_times", "gtfs_shapes", "rt_vehicle_position",
                  "rt_vehicle_latest", "rt_poll_log", "meta",
                  "analytics_grid_hour", "analytics_route_hour")

    def _seed_postgres():
        with pg.pool().connection() as conn:
            conn.execute("TRUNCATE " + ", ".join(_PG_TABLES) + " RESTART IDENTITY")
            for table, (cols, rows) in DATASET.items():
                _insert(conn, table, cols, rows, "%s")
            obs = [r + (NOW,) for r in OBSERVATIONS]
            # Carve the daily partitions before the bulk insert, the same way
            # upsert_vehicles() does at runtime - otherwise these rows land in
            # the default partition and later ensure-partition calls collide.
            for day_ts in {(r[1] // 86400) * 86400 for r in obs}:
                conn.execute("SELECT rt_ensure_day_partition(%s)", (day_ts,))
            _insert(conn, "rt_vehicle_position", _VEHICLE_COLS, obs, "%s")
            latest = {}
            for r in obs:
                if r[0] not in latest or r[1] > latest[r[0]][1]:
                    latest[r[0]] = r
            _insert(conn, "rt_vehicle_latest", _VEHICLE_COLS, list(latest.values()), "%s")
            _insert(conn, "rt_poll_log", _POLL_COLS, POLLS, "%s")
            conn.execute("INSERT INTO meta (key, value) VALUES ('static_loaded_at', %s)",
                         (str(NOW - 86400),))

    ADAPTERS.append(("postgres", PostgresRepository, _seed_postgres))


@pytest.fixture(scope="session", autouse=True)
def _schema():
    db.init_db()


@pytest.fixture(params=ADAPTERS, ids=[a[0] for a in ADAPTERS])
def repo(request):
    """A seeded repository, one instance per adapter under test."""
    _name, cls, seed = request.param
    seed()
    return cls()
