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
-- Serves vehicles_in_window()'s DISTINCT ON (vehicle_id) ... ORDER BY
-- vehicle_id, ts DESC - the quality tier's time-ranged "latest position per
-- vehicle in [since, until]" query. Declared on the partitioned parent, so
-- Postgres propagates it to every existing and future day partition.
CREATE INDEX IF NOT EXISTS ix_vp_vehicle_ts ON rt_vehicle_position(vehicle_id, ts DESC);
-- Same idea for distinct_live_trip_keys()'s DISTINCT ON (route_id, trip_id)
-- ... ORDER BY route_id, trip_id, ts DESC (Aggregator._match_new_trips,
-- every 6h). Without this the query had no index touching trip_id at all,
-- so it would fall back to a full sort of the whole [since, until) window -
-- the same shape of unindexed scan on this table that caused the original
-- CPU incident this deployment already lived through. The RANGE partition
-- on ts already prunes to at most the 1-2 day partitions a chunk can span;
-- this index is what makes the DISTINCT ON cheap *within* those.
CREATE INDEX IF NOT EXISTS ix_vp_route_trip_ts ON rt_vehicle_position(route_id, trip_id, ts DESC);

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
    -- Empty partition, so this validates instantly - no NOT VALID needed
    -- here, unlike the one-time backfill over existing partitions above.
    EXECUTE format(
        'ALTER TABLE %I ADD CONSTRAINT fk_vp_route '
        'FOREIGN KEY (route_id) REFERENCES gtfs_routes(route_id)', part_name);
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

-- Foreign key from the live feed's route_id back to the static timetable.
--
-- Deliberately left NO ACTION (the default) on delete, not ON DELETE
-- SET NULL/CASCADE, unlike live_trip_match.matched_trip_id below. The two
-- look like the same situation but aren't: a route_id here is a *fact this
-- service measured* - a bus really did report this route at this time - so
-- silently nulling it out because someone deleted the row from gtfs_routes
-- would quietly corrupt real history. If a future static-feed refresh ever
-- needs to remove a route_id that live data has referenced, that removal
-- SHOULD fail loudly (this FK) rather than succeed by erasing what buses
-- actually reported. A route refresh should be an upsert (INSERT ... ON
-- CONFLICT (route_id) DO UPDATE), never a delete-and-reinsert, for exactly
-- this reason - route_id is meant to be a stable identifier across feed
-- versions in practice anyway. (No such refresh path exists in this
-- codebase yet; this comment is here so the choice is a decision, not an
-- accident, whenever one gets built.)
--
-- This can't be declared once on the partitioned parent the ordinary way:
-- Postgres (through at least 16, which is what's deployed) refuses a
-- NOT VALID foreign key on a partitioned table outright -
-- "This feature is not yet supported on partitioned tables" - and a
-- non-NOT VALID one validates every partition inline, which on
-- rt_vehicle_position's 20M+ rows would hold a lock for the length of that
-- scan during whatever startup runs this. Confirmed against a real
-- Postgres 16 instance while building this, not assumed.
--
-- So the constraint is added per partition instead - each partition is an
-- ordinary table, where NOT VALID is allowed - looping over whatever
-- partitions already exist. Every *future* day's partition gets the same
-- constraint at creation time in rt_ensure_day_partition() below, fully
-- validated there for free since a brand-new partition starts empty.
-- Validating today's already-existing partitions against their (currently
-- 20M+ row) history is left as a deliberate manual step, one partition at a
-- time, off-peak:
--   ALTER TABLE rt_vehicle_position_20260927 VALIDATE CONSTRAINT fk_vp_route;
DO $$
DECLARE
    part regclass;
BEGIN
    FOR part IN
        SELECT i.inhrelid FROM pg_inherits i WHERE i.inhparent = 'rt_vehicle_position'::regclass
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'fk_vp_route' AND conrelid = part
        ) THEN
            EXECUTE format(
                'ALTER TABLE %s ADD CONSTRAINT fk_vp_route '
                'FOREIGN KEY (route_id) REFERENCES gtfs_routes(route_id) NOT VALID',
                part);
        END IF;
    END LOOP;
END $$;

-- Anything the app layer kept out of rt_vehicle_position because its
-- route_id doesn't exist in gtfs_routes - see Repository.upsert_vehicles()
-- for why this is filtered in Python before the insert rather than left to
-- the FK above to reject: a single bad row inside one poll's batched
-- executemany() would otherwise roll back that whole poll's insert, not
-- just the bad row. Same shape as rt_vehicle_position, plus why/when.
CREATE TABLE IF NOT EXISTS rt_vehicle_position_deadletter (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    vehicle_id       text   NOT NULL,
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
    reason           text   NOT NULL,     -- e.g. 'unknown_route_id'
    quarantined_at   bigint NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_vp_dl_route ON rt_vehicle_position_deadletter(route_id);
CREATE INDEX IF NOT EXISTS ix_vp_dl_ts    ON rt_vehicle_position_deadletter(quarantined_at);

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

-- ---------------------------------------------------------------------------
-- Continuity rollup (Layer 3). Deliberately NOT recomputed-whole-bucket like
-- grid/route above: a gap is defined between two consecutive rows, so a
-- bucket can't be derived in isolation without knowing the row before it.
-- These state tables carry that forward; the fold only ever scans the new
-- rows since the last pass (services/aggregator.py), never full history -
-- which is what keeps a LAG() over this table cheap on a day-partitioned
-- table: the batch is always small and almost always single-partition,
-- unlike a live query re-deriving gaps from the whole retained window.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS analytics_vehicle_gap_state (
    vehicle_id text PRIMARY KEY,
    last_ts    bigint NOT NULL
);

CREATE TABLE IF NOT EXISTS analytics_trip_gap_state (
    vehicle_id text    NOT NULL,
    trip_id    text    NOT NULL,
    last_ts    bigint  NOT NULL,
    has_gap    boolean NOT NULL DEFAULT false,
    PRIMARY KEY (vehicle_id, trip_id)
);
CREATE INDEX IF NOT EXISTS ix_trip_gap_last_ts ON analytics_trip_gap_state(last_ts);

CREATE TABLE IF NOT EXISTS analytics_continuity_hour (
    hour_bucket         bigint  NOT NULL,
    gaps_total          integer NOT NULL DEFAULT 0,
    gaps_over_threshold integer NOT NULL DEFAULT 0,
    b_0_60              integer NOT NULL DEFAULT 0,
    b_60_180            integer NOT NULL DEFAULT 0,
    b_180_300           integer NOT NULL DEFAULT 0,
    b_300_600           integer NOT NULL DEFAULT 0,
    b_600_plus          integer NOT NULL DEFAULT 0,
    PRIMARY KEY (hour_bucket)
);
-- Watermark lives in meta, key 'continuity_watermark_ts' - separate from the
-- grid/route watermark ('aggregate_watermark_ts') because this one must never
-- rewind (see comment above): rewinding would double-count into the additive
-- counters here.

-- Derived relationship between a live trip and the static timetable. Can't
-- be a plain FOREIGN KEY on rt_vehicle_position.trip_id the way route_id got
-- one (see fk_vp_route above): a live trip_id encodes the vehicle's actual
-- dispatch time and a running sequence number - it is never equal to a
-- static trip_id, and the feed marks every trip schedule_relationship=ADDED,
-- meaning the provider itself says none of them are scheduled trips. So the
-- match is computed (nearest timetabled departure on the same route, within
-- a tolerance) and stored here instead of enforced as a column constraint.
-- matched_trip_id *is* a real FK, because it points at an actual static
-- trip once a match exists. Populated by Aggregator._match_new_trips() /
-- app/services/trip_matcher.py, not at ingest time - matching is a batched,
-- 6-hourly job for the same reason the rollup is (see aggregator.py).
--
-- ON DELETE SET NULL here, unlike fk_vp_route above - and this asymmetry is
-- deliberate, not an inconsistency. This column isn't a measured fact, it's
-- this service's own best-effort annotation ("we think this live trip
-- corresponds to that scheduled one"). If a static-feed refresh ever removes
-- the matched static trip, the honest answer becomes "we don't have a match
-- for this anymore" - matched_trip_id -> NULL - rather than blocking the
-- refresh outright the way losing a *measured* route_id would. match_type
-- is left as whatever it was: a dangling NULL match_type='strict' row is a
-- readable historical record ("this was a confident match against a
-- timetable that has since changed"), and any live trip still inside the
-- aggregator's watermark window gets a fresh match next tick regardless.
CREATE TABLE IF NOT EXISTS live_trip_match (
    live_trip_id    text   PRIMARY KEY,
    vehicle_id      text   NOT NULL,
    route_id        text   NOT NULL,
    matched_trip_id text   REFERENCES gtfs_trips(trip_id) ON DELETE SET NULL,
    match_type      text   NOT NULL CHECK (match_type IN ('strict', 'relaxed', 'none')),
    delta_minutes   double precision,
    matched_at      bigint NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ltm_vehicle     ON live_trip_match(vehicle_id);
CREATE INDEX IF NOT EXISTS ix_ltm_route       ON live_trip_match(route_id);
CREATE INDEX IF NOT EXISTS ix_ltm_match_type  ON live_trip_match(match_type);
CREATE INDEX IF NOT EXISTS ix_ltm_matched_at  ON live_trip_match(matched_at);
-- Watermark: meta key 'trip_match_watermark_ts', same shape as the grid/route
-- one - rewinds a little on every pass so a trip whose vehicle is still
-- mid-dispatch when first seen gets re-matched once more is-known.
