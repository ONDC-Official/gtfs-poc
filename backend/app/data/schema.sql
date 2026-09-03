-- ===========================================================================
-- Data plane schema.
--   * gtfs_*  : static schedule, loaded once from the GTFS .txt feed
--   * rt_*    : realtime observations, appended by the poller every N seconds
-- Kept deliberately close to the GTFS spec so a swap to Postgres/PostGIS is
-- mechanical (types widen, geometry columns replace lat/lon pairs).
-- ===========================================================================

CREATE TABLE IF NOT EXISTS gtfs_agency (
    agency_id       TEXT PRIMARY KEY,
    agency_name     TEXT,
    agency_url      TEXT,
    agency_timezone TEXT,
    agency_lang     TEXT
);

CREATE TABLE IF NOT EXISTS gtfs_routes (
    route_id         TEXT PRIMARY KEY,
    agency_id        TEXT,
    route_short_name TEXT,
    route_long_name  TEXT,
    route_desc       TEXT,
    route_type       INTEGER
);
CREATE INDEX IF NOT EXISTS ix_routes_short ON gtfs_routes(route_short_name);

CREATE TABLE IF NOT EXISTS gtfs_stops (
    stop_id   TEXT PRIMARY KEY,
    stop_code TEXT,
    stop_name TEXT,
    stop_lat  REAL,
    stop_lon  REAL
);
CREATE INDEX IF NOT EXISTS ix_stops_latlon ON gtfs_stops(stop_lat, stop_lon);

CREATE TABLE IF NOT EXISTS gtfs_trips (
    trip_id       TEXT PRIMARY KEY,
    route_id      TEXT,
    service_id    TEXT,
    trip_headsign TEXT,
    direction_id  INTEGER,
    shape_id      TEXT
);
CREATE INDEX IF NOT EXISTS ix_trips_route ON gtfs_trips(route_id);
CREATE INDEX IF NOT EXISTS ix_trips_shape ON gtfs_trips(shape_id);

CREATE TABLE IF NOT EXISTS gtfs_stop_times (
    trip_id        TEXT,
    stop_id        TEXT,
    stop_sequence  INTEGER,
    arrival_time   TEXT,
    departure_time TEXT,
    -- seconds past midnight; GTFS allows >24h so we keep the raw text too
    arrival_s      INTEGER,
    departure_s    INTEGER,
    PRIMARY KEY (trip_id, stop_sequence)
);
CREATE INDEX IF NOT EXISTS ix_stop_times_stop ON gtfs_stop_times(stop_id, departure_s);
CREATE INDEX IF NOT EXISTS ix_stop_times_dep  ON gtfs_stop_times(departure_s);

CREATE TABLE IF NOT EXISTS gtfs_shapes (
    shape_id           TEXT,
    shape_pt_sequence  INTEGER,
    shape_pt_lat       REAL,
    shape_pt_lon       REAL,
    shape_dist_traveled REAL,
    PRIMARY KEY (shape_id, shape_pt_sequence)
);

-- --------------------------------------------------------------------------
-- Realtime
-- --------------------------------------------------------------------------

-- Append-only observation log. One row per vehicle per poll (deduped on
-- (vehicle_id, ts) so a stale feed replay does not inflate the history).
CREATE TABLE IF NOT EXISTS rt_vehicle_position (
    vehicle_id   TEXT NOT NULL,
    ts           INTEGER NOT NULL,      -- unix seconds, vehicle timestamp
    trip_id      TEXT,
    route_id     TEXT,
    lat          REAL,
    lon          REAL,
    bearing      REAL,
    speed        REAL,                  -- m/s as reported by the feed
    stop_id      TEXT,
    current_status INTEGER,
    congestion_level INTEGER,
    occupancy_status INTEGER,
    ingested_at  INTEGER NOT NULL,      -- unix seconds, when we stored it
    PRIMARY KEY (vehicle_id, ts)
);
CREATE INDEX IF NOT EXISTS ix_vp_ts       ON rt_vehicle_position(ts);
CREATE INDEX IF NOT EXISTS ix_vp_route_ts ON rt_vehicle_position(route_id, ts);
CREATE INDEX IF NOT EXISTS ix_vp_ingested ON rt_vehicle_position(ingested_at);

-- Current-state projection: exactly one row per vehicle, upserted each poll.
-- Keeps the hot "what is on the map right now" read off the history table.
CREATE TABLE IF NOT EXISTS rt_vehicle_latest (
    vehicle_id   TEXT PRIMARY KEY,
    ts           INTEGER NOT NULL,
    trip_id      TEXT,
    route_id     TEXT,
    lat          REAL,
    lon          REAL,
    bearing      REAL,
    speed        REAL,
    stop_id      TEXT,
    current_status INTEGER,
    congestion_level INTEGER,
    occupancy_status INTEGER,
    ingested_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_latest_route ON rt_vehicle_latest(route_id);
CREATE INDEX IF NOT EXISTS ix_latest_ts    ON rt_vehicle_latest(ts);

-- One row per poll attempt. Powers the feed-health panel and lets the
-- analytics tier reason about gaps in coverage.
CREATE TABLE IF NOT EXISTS rt_poll_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    polled_at     INTEGER NOT NULL,
    ok            INTEGER NOT NULL,
    source        TEXT NOT NULL,        -- 'feed' | 'mock'
    http_status   INTEGER,
    entity_count  INTEGER,
    new_rows      INTEGER,
    feed_timestamp INTEGER,
    latency_ms    INTEGER,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS ix_poll_at ON rt_poll_log(polled_at);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- ===========================================================================
-- Analytics tier: rollups.
--
-- The realtime tables are an event log - correct, but too slow to drive a map
-- that redraws on every filter change. These tables are derived state: a
-- background aggregator folds new observations into them incrementally and
-- the analytics API reads only from here.
--
-- Grid cells are stored at the FINEST resolution and rolled up to coarser
-- ones by integer division of the indices (500 m = /2, 1 km = /4), so one
-- write path serves every zoom level.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS analytics_grid_hour (
    cell_y       INTEGER NOT NULL,   -- floor(lat / GRID_LAT_DEG)
    cell_x       INTEGER NOT NULL,   -- floor(lon / GRID_LON_DEG)
    hour_bucket  INTEGER NOT NULL,   -- unix seconds truncated to the hour
    observations INTEGER NOT NULL DEFAULT 0,
    vehicles     INTEGER NOT NULL DEFAULT 0,  -- distinct within this cell-hour
    moving       INTEGER NOT NULL DEFAULT 0,
    stopped      INTEGER NOT NULL DEFAULT 0,
    speed_sum    REAL    NOT NULL DEFAULT 0,  -- moving observations only
    speed_n      INTEGER NOT NULL DEFAULT 0,
    speed_min    REAL,
    speed_max    REAL,
    PRIMARY KEY (cell_y, cell_x, hour_bucket)
);
CREATE INDEX IF NOT EXISTS ix_grid_hour ON analytics_grid_hour(hour_bucket);

CREATE TABLE IF NOT EXISTS analytics_route_hour (
    route_id     TEXT    NOT NULL,
    hour_bucket  INTEGER NOT NULL,
    observations INTEGER NOT NULL DEFAULT 0,
    vehicles     INTEGER NOT NULL DEFAULT 0,
    moving       INTEGER NOT NULL DEFAULT 0,
    stopped      INTEGER NOT NULL DEFAULT 0,
    speed_sum    REAL    NOT NULL DEFAULT 0,
    speed_n      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (route_id, hour_bucket)
);
CREATE INDEX IF NOT EXISTS ix_route_hour ON analytics_route_hour(hour_bucket);

-- Watermark for the incremental rollup, so a restart resumes rather than
-- rescanning the whole history. Key: 'aggregate_watermark_ts'.
