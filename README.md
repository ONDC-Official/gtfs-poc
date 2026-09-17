# Delhi Bus Network — live monitoring + analytics

Three-tier app over the Delhi GTFS static feed and a GTFS-realtime vehicle-position feed.

```
frontend/           UI tier      React 19 + Vite + MapLibre + Recharts
                                 Live view (fleet now) + Analytics view (history)
backend/app/api     service tier FastAPI: REST + websocket, no business logic
backend/app/services             realtime.py     ingestion poller
                                 live_analytics  snapshot metrics
                                 aggregator.py   rollup writer  } analytics
                                 spatial_analytics.py  rollup reader } tier
backend/app/data    data plane   Repository port + SQLite adapter + schema
data/gtfs.db                     3.2M static rows, realtime log, analytics rollups
```

The tiers talk through interfaces, not imports: the API layer calls services, services
call `data.repository.Repository`. Swapping SQLite for Postgres/PostGIS means writing
one new `Repository` implementation — no service or API changes.

## Run it (without Docker)

Two terminals, from the repo root:

```bash
# 1. service tier  (http://127.0.0.1:8000)
./.venv/bin/python -m uvicorn app.main:app --reload --app-dir backend --port 8000

# 2. UI tier       (http://localhost:5173)
cd frontend && npm run dev
```

Open **http://localhost:5173**. Vite proxies `/api` and `/ws` to the backend, so the
browser stays same-origin.

Build the database from the static feed (~9s, produces a ~300 MB SQLite file in `data/`):

```bash
cd backend && ../.venv/bin/python scripts/load_static.py
```

## Connecting the live feed

Without a key the poller serves a **simulated feed** — vehicles driven along real
shapes from the static data — so the whole pipeline is demonstrable offline. The
header shows a `SIMULATED FEED` badge whenever this is active.

**Your feed is already configured** in `backend/.env`, read from `gtfs-rt.cred`. It uses
the TransportStack gateway, which authenticates with an `x-api-key` **header** rather
than a query parameter:

```ini
RT_API_KEY=<your key>
RT_API_KEY_HEADER=x-api-key
RT_VEHICLE_POSITIONS_URL=https://dts-backend.transportstack.in/api/dataset/otd/get-file?agency=delhi-buses&category=realtime_gtfs&filename=VehiclePositions.pb
RT_POLL_SECONDS=30
```

It currently returns **~5,700 live vehicles per poll**. If a feed wants the key on the
query string instead, clear `RT_API_KEY_HEADER` and set `RT_API_KEY_PARAM`.

`POST /api/realtime/poll` forces an immediate poll — the fastest way to check a key.
Feed errors surface in the Feed panel with the cause (bad key, rate limit, non-protobuf
response) rather than a raw HTML dump.

Both `gtfs-rt.cred` and `backend/.env` are in `.gitignore`.

## What the poller does every 30s

1. Fetch and parse the VehiclePositions protobuf.
2. Append observations to `rt_vehicle_position` (dedup on `vehicle_id, ts`) and upsert
   the current-state projection `rt_vehicle_latest`.
3. Record the attempt in `rt_poll_log` — latency, entity count, HTTP status, error.
4. Push the batch to every connected websocket client.

History older than `HISTORY_RETENTION_HOURS` (default **12**) is pruned hourly.

**Watch the growth.** At ~5,700 vehicles on a 30s cadence the history table takes on the
order of 16M rows/day — roughly 2.5 GB in SQLite. 12 hours is a deliberate default, not
a limit; raise it once the history lives somewhere built for it (see below), or widen
`RT_POLL_SECONDS` if you only need coarse trends.

## Getting the data

Two inputs, neither of them in this repo.

**1. The static schedule.** Download the Delhi buses GTFS feed from
[Open Transit Data Delhi](https://otd.delhi.gov.in/) and unzip it so the `.txt` files sit
directly in `delhi_buses_static_gtfs_v1-2/`:

```
delhi_buses_static_gtfs_v1-2/
  agency.txt  calendar.txt  feed_info.txt  routes.txt
  shapes.txt  stop_times.txt  stops.txt  trips.txt
```

It is ~124 MB (`stop_times.txt` alone is 89 MB), which is why it is fetched rather than
committed. Point `GTFS_STATIC_DIR` elsewhere if you keep it somewhere else. Any GTFS feed
with the same files works — nothing here is Delhi-specific except the default map centre.

**2. A GTFS-realtime key.** Register at Open Transit Data for a vehicle-positions feed,
then create `backend/.env`:

```ini
RT_API_KEY=your_key_here
RT_API_KEY_HEADER=x-api-key
RT_VEHICLE_POSITIONS_URL=https://your-gateway/VehiclePositions.pb
RT_POLL_SECONDS=30
```

**Without a key the app still runs.** The poller falls back to a simulated feed —
vehicles driven along real shapes from the static schedule — so the whole pipeline is
demonstrable offline. A `SIMULATED FEED` badge sits in the header whenever that is what
you are looking at.

## Deploy with Docker

```bash
cp .env.example .env          # optional: ports, static-feed path
docker compose up -d --build  # first run loads the GTFS static feed (~8s)
```

The static feed must be in place first — see **Getting the data** above.

Then open **http://localhost:8080**.

Three services, matching the three tiers:

| Service | Tier | Role |
|---|---|---|
| `loader` | data plane | one-shot: GTFS `.txt` → SQLite in the `gtfs-data` volume, then exits |
| `api` | service | FastAPI + the 30s realtime poller |
| `web` | UI | nginx serving the React bundle and fronting `/api` and `/ws` |

`api` waits for `loader` to complete successfully; `web` waits for `api` to be
*healthy*, so a `docker compose up` either comes up working or fails loudly.

**The feed key.** `api` reads `backend/.env` and then the repo-root `.env`, in that
order, so an existing local setup containers unchanged. Secrets are never baked into
the image — `.dockerignore` excludes them, and both files are gitignored. Nothing in
`.env.example` is uncommented for the feed, because a later empty value would override
a working key and silently drop you onto the simulated feed.

**Static data** is bind-mounted read-only from `GTFS_STATIC_DIR` (default
`./delhi_buses_static_gtfs_v1-2`), so a newer GTFS release needs no rebuild — drop the
files in and run `GTFS_FORCE_RELOAD=1 docker compose up -d loader`. The loader is
idempotent and skips when the database already carries the feed.

**Ports.** Only `web` is published by default (`WEB_PORT`, 8080). The API binds to
`127.0.0.1:8000` for direct access and debugging; set `API_PORT=0.0.0.0:8000` to expose
it, or drop the mapping entirely — the UI does not need it.

Useful commands:

```bash
docker compose logs -f api            # poller activity and feed errors
docker compose ps                     # health status
docker compose exec api sh            # the database is at /data/gtfs.db
GTFS_FORCE_RELOAD=1 docker compose up -d loader   # rebuild from static feed
docker compose down                   # stop; the volume survives
docker compose down -v                # stop and delete the database
```

### Before putting it on the internet

The compose file is a complete single-host deployment, not a hardened one. What it
does have: non-root API user, healthchecks on both services, `unless-stopped` restart,
capped container logs, loopback-only API, and no secrets in the images. What you still
need to add:

- **TLS.** nginx serves plain HTTP. Terminate TLS in front of it, or add a certificate
  and a 443 server block. The websocket then needs `wss://`, which the client already
  selects from `window.location.protocol`.
- **Backups**, if the realtime history matters. It lives only in the `gtfs-data` volume.
- **A real database** if you run more than one API replica. SQLite in a volume is
  single-writer, and the poller is the writer — scale the API out and you would need
  the Postgres adapter behind `Repository`.

## The analytics tier

The realtime tables are an event log — correct, but too slow to drive a map that
redraws on every control change. So the analytics tier keeps **derived state**:

```
rt_vehicle_position  ──aggregator──▶  analytics_grid_hour    ──▶  spatial_analytics ──▶ API
   (event log)         every 120s     analytics_route_hour        (read side)
```

`aggregator.py` folds new observations into hourly rollups — a 250 m spatial grid and a
per-route table. It is incremental (a watermark records how far the log is consumed) and
idempotent (each pass recomputes whole hour buckets rather than adding deltas, and the
newest bucket is always re-folded because observations keep arriving after it opens).
Folding ~420k observations takes about 700 ms; a steady-state pass is well under half that.

Cells are stored at the **finest** resolution and rolled up to coarser ones by integer
division of the indices (500 m = /2, 1 km = /4), so one write path serves every zoom.
The lon step is widened by 1/cos(lat) so cells are square over Delhi rather than stretched.

It runs in the API process. SQLite takes one writer at a time and the poller is already
that writer, so a second process would only contend for the lock — `run_once()` is the
seam a separate service would call once the store is Postgres.

### The map dashboard

Three metrics over the same grid, one sequential blue ramp stepped for the dark surface
(verified monotonic in lightness, 1.46:1 → 13.16:1 against the chart surface). The
invariant is that **brighter always means more of the thing the metric names** — so
congestion ramps against *falling* speed, and opacity follows brightness rather than the
raw value, or the most important cells would come out the faintest.

Cells are drawn as soft overlapping circles sized to the ground area they cover, not as
filled squares — a grid of hard rectangles reads as blocks rather than as a phenomenon.
A pixel floor keeps neighbours merging into one surface when zoomed out, and the
geographic term takes over once cells have real size on screen.

| Metric | Bright means | Notes |
|---|---|---|
| Congestion | slower moving traffic | scaled p10–p90 so outlier cells don't flatten the ramp |
| Activity | more observations | scaled to p90; terminals are an order of magnitude above the rest |
| Dwell | buses more often stationary | 0–100% of observations |

### Saying what the words mean

"Stale", "bunched", "dwell", "crawling" are not GTFS terms — they are thresholds this
app chose. Every one of them is defined in `frontend/src/glossary.ts` and shown on hover
against the label it describes, with the actual rule underneath (`speed > 0.5 m/s`,
`now − last report > 5 minutes`, `same route, < 300 m`). One source of truth, so a
threshold cannot drift between the code and its explanation.

Tooltips render through a portal: panels clip with `overflow: hidden` for their rounded
headers and the sidebar scrolls, so an in-flow tooltip is cut to a sliver by one or the
other.

**Hotspots** are ranked cells labelled with the nearest scheduled stop, so a row reads as
a place rather than a coordinate. Two guards keep the congestion list honest:

- cells stationary more than 35% of the time are excluded — those are depots, not jams;
- a cell must actually be below **15 km/h** to qualify. Ranking by ascending speed always
  returns ten rows, even at 3 am when the "slowest" cell is doing 34 km/h. Overnight the
  list is correctly empty.

**Route corridor** plots one route's observed speed cell by cell, read straight from the
log — a single route is small enough not to need a rollup, which is why the grid does not
carry a route dimension.

Endpoints: `/api/analytics/grid`, `/hotspots`, `/corridor/{route_id}`, `/hourly`,
`/aggregator` (status) and `POST /aggregator/run?full=true` to rebuild.

## What the RT screen shows, and why

The feed constrains this. Measured against a live snapshot:

| Field | Populated | Consequence |
|---|---|---|
| `route_id` | 100%, joins to static | the backbone of every route metric |
| `position`, `speed`, `bearing` | 100% | motion, congestion, bunching |
| `trip_id` | 96%, format `{route}_{HH}_{MM}_{seq}` | dispatch hour only — **does not join** to `gtfs_trips` (4.5% even after stripping the suffix) |
| `stop_id` | **0%** | no stop-arrival or dwell metrics |
| `occupancy_status`, `congestion_level` | **0%** | no crowding metrics |
| `current_status` | **0%** | no at-stop state |

So there is **no schedule-adherence metric** here, and there cannot be one until the
feed carries a joinable trip id. The Health tab states this on screen rather than
letting a plausible-looking number imply otherwise.

What the data *does* support, and what the dashboard computes:

- **Bunching** — buses on one route within 300 m of each other. Roughly a third of the
  fleet at peak. The classic headway-collapse signal, and it needs only positions.
- **Congestion** — slowest routes by the average speed of buses *actually moving*
  (>0.5 m/s), on routes with at least 3 in motion. Averaging in parked buses just
  surfaces depots.
- **Network coverage** — ~56% of the 2,554 scheduled routes have a vehicle reporting.
  The Health tab lists the dark ones, busiest schedule first.
- **Fleet state** — moving / stopped / stale, as one meter.
- **Dispatch profile** — when each reporting bus began its run, from the trip id.
- **Data quality** — ~8% of observations report >72 km/h (the feed clamps at 50 m/s);
  these are excluded from speed statistics rather than silently averaged in.

Filters (operator, motion, reporting freshness, bunched, crawling, route) apply to the
map and the counts together, client-side, so they respond instantly across ~6k vehicles.
Bunching is grid-hashed in both tiers; the thresholds live in `filters.ts` and
`live_analytics.py` and are kept in step.

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | liveness + feed status |
| `GET /api/feed` | static feed row counts and bbox |
| `GET /api/routes?q=` | route search |
| `GET /api/routes/{id}/shape` | corridor as GeoJSON LineString |
| `GET /api/routes/{id}/stops` | ordered stops for the representative trip |
| `GET /api/stops?q=&bbox=` | stop search |
| `GET /api/stops/{id}/schedule` | scheduled departures |
| `GET /api/realtime/vehicles` | current fleet snapshot |
| `GET /api/realtime/vehicles/{id}/history` | one vehicle's track as GeoJSON |
| `POST /api/realtime/poll` | force a poll |
| `GET /api/analytics/live` | whole RT dashboard from one snapshot |
| `GET /api/analytics/dark-routes` | scheduled routes with nothing reporting |
| `GET /api/analytics/overview` | fleet KPIs |
| `GET /api/analytics/routes/top` | busiest routes now |
| `GET /api/analytics/ingest` | vehicles reporting over time |
| `GET /api/analytics/speed` | speed histogram |
| `GET /api/analytics/feed-health` | recent poll log |
| `WS /ws/vehicles` | snapshot on connect, then one frame per poll |

Interactive docs at http://127.0.0.1:8000/docs.

## Next: the analytics tier

The data plane already records everything the analytics work needs — a timestamped
position history joined to the full schedule. The natural next steps:

- **Schedule adherence** — blocked on the feed. The live `trip_id` does not join to
  `gtfs_trips`, so this needs either a corrected feed or a fuzzy match of
  route + dispatch time to the nearest scheduled departure. Worth doing, but it is
  inference, not measurement, and should be labelled as such.
- **Headway over time.** Bunching is currently a snapshot. The position history
  supports tracking how bunching on a corridor develops across the day.
- **Day-over-day comparison.** The rollups make "this Tuesday 18:00 vs last" a cheap
  query, but retention is 12h by default — raise it, or roll the hourly buckets up into
  a daily table before they are pruned.
- **Extract the aggregator.** `run_once()` is already the seam; it needs a store that
  takes concurrent writers before it earns its own process.
- **Corridor speed profiles.** Segment-level speeds by hour of day from the position
  history, which is where PostGIS starts to earn its place.
- **Move history to a columnar store.** `rt_vehicle_position` grows ~600 rows/poll;
  DuckDB or Timescale behind the same `Repository` port keeps these queries fast.
