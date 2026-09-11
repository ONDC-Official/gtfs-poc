-- ===========================================================================
-- Data plane schema - PostgreSQL + PostGIS.
--
-- A faithful translation of schema.sql (SQLite). Table and column names are
-- identical to the GTFS spec so the two adapters stay interchangeable; the
-- Repository contract suite runs against both.
--
-- What differs from the SQLite schema, and why:
--   * lat/lon pairs are KEPT, and a generated geometry(Point,4326) column is
--     added alongside each, with a GiST index. Keeping the raw columns means
--     every existing query and response shape is untouched; the geometry is
--     what ST_DWithin / ST_Distance use for the nearby-routes path.
--   * rt_vehicle_position is RANGE-partitioned by day on ts. At ~16M rows/day
--     a plain DELETE for retention means sustained vacuum pressure; dropping a
--     whole day's partition is O(1) and bloat-free. The composite PK includes
--     ts, which is what makes it a legal partition key.
--   * INTEGER widens to BIGINT only for the unix-epoch columns (ts, *_at,
--     hour_bucket, feed_timestamp) - small enumerations stay INTEGER.
--   * AUTOINCREMENT -> GENERATED ALWAYS AS IDENTITY.
--
-- Idempotent: safe to run on an existing database (IF NOT EXISTS throughout,
-- and the partition helper swallows duplicate_table).
-- ===========================================================================

CREATE EXTENSION IF NOT EXISTS postgis;

-- --------------------------------------------------------------------------
-- Static schedule
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS gtfs_agency (
    agency_id       text PRIMARY KEY,
    agency_name     text,
    agency_url      text,
    agency_timezone text,
    agency_lang     text
);

CREATE TABLE IF NOT EXISTS gtfs_routes (
    route_id         text PRIMARY KEY,
    agency_id        text,
    route_short_name text,
    route_long_name  text,
    route_desc       text,
    route_type       integer
);
CREATE INDEX IF NOT EXISTS ix_routes_short ON gtfs_routes(route_short_name);

CREATE TABLE IF NOT EXISTS gtfs_stops (
    stop_id   text PRIMARY KEY,
    stop_code text,
    stop_name text,
    stop_lat  double precision,
    stop_lon  double precision,
    -- ST_SetSRID(ST_MakePoint(...)) is immutable, so it is legal in a
    -- generated column; NULL lat/lon yields a NULL geometry, which the GiST
    -- index simply skips.
    geom geometry(Point, 4326) GENERATED ALWAYS AS
        (ST_SetSRID(ST_MakePoint(stop_lon, stop_lat), 4326)) STORED
);
CREATE INDEX IF NOT EXISTS ix_stops_latlon ON gtfs_stops(stop_lat, stop_lon);
CREATE INDEX IF NOT EXISTS ix_stops_geom   ON gtfs_stops USING GIST (geom);

CREATE TABLE IF NOT EXISTS gtfs_trips (
    trip_id       text PRIMARY KEY,
    route_id      text,
    service_id    text,
    trip_headsign text,
    direction_id  integer,
    shape_id      text
);
CREATE INDEX IF NOT EXISTS ix_trips_route ON gtfs_trips(route_id);
CREATE INDEX IF NOT EXISTS ix_trips_shape ON gtfs_trips(shape_id);

CREATE TABLE IF NOT EXISTS gtfs_stop_times (
    trip_id        text,
    stop_id        text,
    stop_sequence  integer,
    arrival_time   text,
    departure_time text,
    -- seconds past midnight; GTFS allows >24h so the raw text is kept too
    arrival_s      integer,
    departure_s    integer,
    PRIMARY KEY (trip_id, stop_sequence)
);
CREATE INDEX IF NOT EXISTS ix_stop_times_stop ON gtfs_stop_times(stop_id, departure_s);
CREATE INDEX IF NOT EXISTS ix_stop_times_dep  ON gtfs_stop_times(departure_s);

CREATE TABLE IF NOT EXISTS gtfs_shapes (
    shape_id            text,
    shape_pt_sequence   integer,
    shape_pt_lat        double precision,
    shape_pt_lon        double precision,
    shape_dist_traveled double precision,
    geom geometry(Point, 4326) GENERATED ALWAYS AS
        (ST_SetSRID(ST_MakePoint(shape_pt_lon, shape_pt_lat), 4326)) STORED,
    PRIMARY KEY (shape_id, shape_pt_sequence)
);
CREATE INDEX IF NOT EXISTS ix_shapes_geom ON gtfs_shapes USING GIST (geom);

-- --------------------------------------------------------------------------
-- Realtime
-- --------------------------------------------------------------------------

-- Append-only observation log, RANGE-partitioned by day on ts. One row per
-- vehicle per poll, deduped on (vehicle_id, ts). The PK carries ts so it is a
-- valid partition key and ON CONFLICT (vehicle_id, ts) still works.
CREATE TABLE IF NOT EXISTS rt_vehicle_position (
    vehicle_id       text   NOT NULL,
    ts               bigint NOT NULL,      -- unix seconds, vehicle timestamp
    trip_id          text,
    route_id         text,
    lat              double precision,
    lon              double precision,
    bearing          double precision,
    speed            double precision,     -- m/s as reported by the feed
    stop_id          text,
    current_status   integer,
    congestion_level integer,
    occupancy_status integer,
    ingested_at      bigint NOT NULL,      -- unix seconds, when we stored it
    geom geometry(Point, 4326) GENERATED ALWAYS AS
        (ST_SetSRID(ST_MakePoint(lon, lat), 4326)) STORED,
    PRIMARY KEY (vehicle_id, ts)
) PARTITION BY RANGE (ts);

-- Catch-all so a write never fails if a daily partition has not been created
-- yet. rt_ensure_day_partition() moves rows into dated partitions ahead of
-- time; anything that slips through lands here.
CREATE TABLE IF NOT EXISTS rt_vehicle_position_default
    PARTITION OF rt_vehicle_position DEFAULT;

CREATE INDEX IF NOT EXISTS ix_vp_ts       ON rt_vehicle_position(ts);
CREATE INDEX IF NOT EXISTS ix_vp_route_ts ON rt_vehicle_position(route_id, ts);
CREATE INDEX IF NOT EXISTS ix_vp_ingested ON rt_vehicle_position(ingested_at);
CREATE INDEX IF NOT EXISTS ix_vp_geom     ON rt_vehicle_position USING GIST (geom);

-- Create the daily partition covering the given unix-second timestamp, if it
-- does not already exist. Idempotent - a lost race just raises
-- duplicate_table, which is swallowed. Called by upsert_vehicles() for the
-- span of each incoming batch.
CREATE OR REPLACE FUNCTION rt_ensure_day_partition(p_ts bigint)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    day_start bigint := (p_ts / 86400) * 86400;
    day_end   bigint := day_start + 86400;
    part_name text   := 'rt_vehicle_position_' ||
                        to_char(to_timestamp(day_start) AT TIME ZONE 'UTC', 'YYYYMMDD');
BEGIN
    EXECUTE format(
        'CREATE TABLE %I PARTITION OF rt_vehicle_position FOR VALUES FROM (%s) TO (%s)',
        part_name, day_start, day_end);
EXCEPTION
    WHEN duplicate_table THEN NULL;           -- already carved
    WHEN invalid_object_definition THEN NULL; -- overlaps an existing partition
    WHEN check_violation THEN NULL;           -- default already holds this day's
                                              -- rows (a migration / bulk load
                                              -- that bypassed this helper); the
                                              -- rows stay valid in default, we
                                              -- just forgo DROP for that day.
END;
$$;

-- Drop every daily partition whose whole range is older than the cutoff.
-- This is the bloat-free half of retention; prune_history() also DELETEs the
-- straggler rows in the boundary partition and the default partition.
CREATE OR REPLACE FUNCTION rt_drop_old_partitions(p_cutoff bigint)
RETURNS integer LANGUAGE plpgsql AS $$
DECLARE
    r        record;
    dropped  integer := 0;
    hi       bigint;
BEGIN
    FOR r IN
        SELECT c.relname,
               pg_get_expr(c.relpartbound, c.oid) AS bound
        FROM pg_inherits i
        JOIN pg_class c      ON c.oid = i.inhrelid
        JOIN pg_class parent ON parent.oid = i.inhparent
        WHERE parent.relname = 'rt_vehicle_position'
          AND c.relname <> 'rt_vehicle_position_default'
    LOOP
        -- bound looks like: FOR VALUES FROM ('1700000000') TO ('1700086400')
        hi := substring(r.bound from 'TO \(''?([0-9]+)''?\)')::bigint;
        IF hi IS NOT NULL AND hi <= p_cutoff THEN
            EXECUTE format('DROP TABLE %I', r.relname);
            dropped := dropped + 1;
        END IF;
    END LOOP;
    RETURN dropped;
END;
$$;

-- Current-state projection: exactly one row per vehicle, upserted each poll.
CREATE TABLE IF NOT EXISTS rt_vehicle_latest (
    vehicle_id       text PRIMARY KEY,
    ts               bigint NOT NULL,
    trip_id          text,
    route_id         text,
    lat              double precision,
    lon              double precision,
    bearing          double precision,
    speed            double precision,
    stop_id          text,
    current_status   integer,
    congestion_level integer,
    occupancy_status integer,
    ingested_at      bigint NOT NULL,
    geom geometry(Point, 4326) GENERATED ALWAYS AS
        (ST_SetSRID(ST_MakePoint(lon, lat), 4326)) STORED
);
CREATE INDEX IF NOT EXISTS ix_latest_route ON rt_vehicle_latest(route_id);
CREATE INDEX IF NOT EXISTS ix_latest_ts    ON rt_vehicle_latest(ts);
CREATE INDEX IF NOT EXISTS ix_latest_geom  ON rt_vehicle_latest USING GIST (geom);

CREATE TABLE IF NOT EXISTS rt_poll_log (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    polled_at      bigint  NOT NULL,
    ok             integer NOT NULL,
    source         text    NOT NULL,       -- 'feed' | 'mock'
    http_status    integer,
    entity_count   integer,
    new_rows       integer,
    feed_timestamp bigint,
    latency_ms     integer,
    error          text
);
CREATE INDEX IF NOT EXISTS ix_poll_at ON rt_poll_log(polled_at);

CREATE TABLE IF NOT EXISTS meta (
    key   text PRIMARY KEY,
    value text
);

-- ===========================================================================
-- Analytics tier: rollups. Logic unchanged from the SQLite schema - the
-- aggregator recomputes whole hour buckets, so these stay plain tables.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS analytics_grid_hour (
    cell_y       integer NOT NULL,   -- floor(lat / GRID_LAT_DEG)
    cell_x       integer NOT NULL,   -- floor(lon / GRID_LON_DEG)
    hour_bucket  bigint  NOT NULL,   -- unix seconds truncated to the hour
    observations integer NOT NULL DEFAULT 0,
    vehicles     integer NOT NULL DEFAULT 0,
    moving       integer NOT NULL DEFAULT 0,
    stopped      integer NOT NULL DEFAULT 0,
    speed_sum    double precision NOT NULL DEFAULT 0,
    speed_n      integer NOT NULL DEFAULT 0,
    speed_min    double precision,
    speed_max    double precision,
    PRIMARY KEY (cell_y, cell_x, hour_bucket)
);
CREATE INDEX IF NOT EXISTS ix_grid_hour ON analytics_grid_hour(hour_bucket);

CREATE TABLE IF NOT EXISTS analytics_route_hour (
    route_id     text    NOT NULL,
    hour_bucket  bigint  NOT NULL,
    observations integer NOT NULL DEFAULT 0,
    vehicles     integer NOT NULL DEFAULT 0,
    moving       integer NOT NULL DEFAULT 0,
    stopped      integer NOT NULL DEFAULT 0,
    speed_sum    double precision NOT NULL DEFAULT 0,
    speed_n      integer NOT NULL DEFAULT 0,
    PRIMARY KEY (route_id, hour_bucket)
);
CREATE INDEX IF NOT EXISTS ix_route_hour ON analytics_route_hour(hour_bucket);
