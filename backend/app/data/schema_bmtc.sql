-- BMTC (Bengaluru) collector schema. Applied by app/bmtc/collector.py on
-- start; idempotent (IF NOT EXISTS / CREATE OR REPLACE throughout).
--
-- Lives in its own schema so BMTC ids never collide with the DTC tables in
-- `public`, and nothing here is read or written by the DTC API.
--
--   * vehicle_position is RANGE-partitioned by HOUR on ts, so 72h retention
--     is DROP TABLE of whole hours - O(1), no DELETE over millions of rows on
--     a CPU-constrained host.
--   * Links to routes/trips/stops are logical, not FOREIGN KEYs: the live
--     feed references trips missing from the current static pull (~3%), and
--     those positions are still real.

CREATE SCHEMA IF NOT EXISTS bmtc;

-- --------------------------------------------------------------------------
-- Live
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS bmtc.vehicle_position (
    vehicle_id   text        NOT NULL,
    ts           timestamptz NOT NULL,   -- feed timestamp minus the IST offset
    lat          double precision NOT NULL,
    lon          double precision NOT NULL,
    bearing      real,                   -- 0.0 from the feed stored as NULL
    trip_id      text,
    route_id     text,
    start_time   text,
    start_date   date,
    scheduled    boolean,                -- schedule_relationship = SCHEDULED
    sch_no       text,                   -- 'N/A' stored as NULL
    stop_id      text,
    status       smallint,               -- current_status: 1 stopped at, 2 in transit to
    poll_id      bigint,
    ingested_at  timestamptz NOT NULL,
    PRIMARY KEY (vehicle_id, ts)
) PARTITION BY RANGE (ts);

CREATE TABLE IF NOT EXISTS bmtc.vehicle_position_default
    PARTITION OF bmtc.vehicle_position DEFAULT;

CREATE INDEX IF NOT EXISTS ix_bmtc_vp_ts ON bmtc.vehicle_position (ts);

CREATE TABLE IF NOT EXISTS bmtc.vehicles (
    vehicle_id  text PRIMARY KEY,
    label       text,                    -- registration plate
    first_seen  timestamptz NOT NULL,
    last_seen   timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS bmtc.vehicle_latest (
    vehicle_id   text PRIMARY KEY,
    ts           timestamptz NOT NULL,
    lat          double precision NOT NULL,
    lon          double precision NOT NULL,
    bearing      real,
    trip_id      text,
    route_id     text,
    sch_no       text,
    stop_id      text,
    status       smallint,
    ingested_at  timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS bmtc.poll_log (
    poll_id      bigserial PRIMARY KEY,
    polled_at    timestamptz NOT NULL DEFAULT now(),
    endpoint     text        NOT NULL,   -- vehicle | trip_update
    http_status  integer,
    is_success   boolean,
    error        text,
    header_ts    timestamptz,
    entities     integer,
    new_rows     integer,
    duplicates   integer,
    stale        integer,                -- older than retention, not stored
    latency_ms   integer
);
CREATE INDEX IF NOT EXISTS ix_bmtc_poll_at ON bmtc.poll_log (polled_at);

-- Empty until BMTC populates the feed. Columns follow GTFS-RT TripUpdate;
-- per-stop predictions kept as JSON since the shape is not yet observed.
CREATE TABLE IF NOT EXISTS bmtc.trip_update (
    trip_id            text        NOT NULL,
    update_ts          timestamptz NOT NULL,
    route_id           text,
    start_time         text,
    start_date         date,
    schedule_relationship text,
    vehicle_id         text,
    delay              integer,
    stop_time_updates  jsonb,
    poll_id            bigint,
    PRIMARY KEY (trip_id, update_ts)
);

-- --------------------------------------------------------------------------
-- Static (replaced wholesale every BMTC_STATIC_EVERY_HOURS)
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS bmtc.agency (
    agency_id text PRIMARY KEY, agency_name text, agency_url text,
    agency_timezone text, agency_phone text, agency_lang text);

CREATE TABLE IF NOT EXISTS bmtc.stops (
    stop_id text PRIMARY KEY, stop_name text,
    stop_lat double precision, stop_lon double precision);

CREATE TABLE IF NOT EXISTS bmtc.routes (
    route_id text PRIMARY KEY, agency_id text, route_short_name text,
    route_long_name text, route_desc text, route_type smallint);

CREATE TABLE IF NOT EXISTS bmtc.trips (
    trip_id text PRIMARY KEY, route_id text, service_id text);
CREATE INDEX IF NOT EXISTS ix_bmtc_trips_route ON bmtc.trips (route_id);

CREATE TABLE IF NOT EXISTS bmtc.stop_times (
    trip_id text NOT NULL, stop_sequence integer NOT NULL,
    arrival_time text, departure_time text, stop_id text,
    PRIMARY KEY (trip_id, stop_sequence));

CREATE TABLE IF NOT EXISTS bmtc.calendar (
    service_id text PRIMARY KEY,
    monday boolean, tuesday boolean, wednesday boolean, thursday boolean,
    friday boolean, saturday boolean, sunday boolean,
    start_date date, end_date date);         -- '000000' from the feed -> NULL

CREATE TABLE IF NOT EXISTS bmtc.static_pull_log (
    pull_id      bigserial PRIMARY KEY,
    pulled_at    timestamptz NOT NULL DEFAULT now(),
    check_no     smallint    NOT NULL,
    file_name    text        NOT NULL,
    http_status  integer,
    row_count    integer,
    warnings     text
);

-- --------------------------------------------------------------------------
-- Partition helpers
-- --------------------------------------------------------------------------

-- Create the hourly partition covering p_ts, if missing. A lost race or an
-- hour already held by the default partition is swallowed; those rows stay
-- valid in default and are removed by the DELETE half of retention.
CREATE OR REPLACE FUNCTION bmtc.ensure_hour_partition(p_ts timestamptz)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    h_start timestamptz := date_trunc('hour', p_ts AT TIME ZONE 'UTC') AT TIME ZONE 'UTC';
    part    text := 'vehicle_position_' ||
                    to_char(h_start AT TIME ZONE 'UTC', 'YYYYMMDD_HH24');
BEGIN
    EXECUTE format(
        'CREATE TABLE bmtc.%I PARTITION OF bmtc.vehicle_position FOR VALUES FROM (%L) TO (%L)',
        part, h_start, h_start + interval '1 hour');
EXCEPTION
    WHEN duplicate_table THEN NULL;
    WHEN invalid_object_definition THEN NULL;
    WHEN check_violation THEN NULL;
END;
$$;

-- Drop every hourly partition that ends at or before the cutoff.
CREATE OR REPLACE FUNCTION bmtc.drop_old_partitions(p_cutoff timestamptz)
RETURNS integer LANGUAGE plpgsql AS $$
DECLARE
    r       record;
    dropped integer := 0;
    part_hour timestamptz;
BEGIN
    FOR r IN
        SELECT c.relname
        FROM pg_inherits i
        JOIN pg_class c      ON c.oid = i.inhrelid
        JOIN pg_class parent ON parent.oid = i.inhparent
        JOIN pg_namespace n  ON n.oid = parent.relnamespace
        WHERE n.nspname = 'bmtc' AND parent.relname = 'vehicle_position'
          AND c.relname ~ '^vehicle_position_[0-9]{8}_[0-9]{2}$'
    LOOP
        -- Partition names are UTC hours (see ensure_hour_partition).
        part_hour :=(substring(r.relname from '([0-9]{8})_')::date
                      + (substring(r.relname from '_([0-9]{2})$') || ' hours')::interval)
                     AT TIME ZONE 'UTC';
        IF part_hour + interval '1 hour' <= p_cutoff THEN
            EXECUTE format('DROP TABLE bmtc.%I', r.relname);
            dropped := dropped + 1;
        END IF;
    END LOOP;
    RETURN dropped;
END;
$$;
