# Perf harness

Performance and stress harness for Phase 1 Transit
(transit-hub-collector and transit-hub-consumer). It implements
`docs/phase1-perf-harness-implementation-plan.md` (milestone M1).

It tests the services from the outside, as a black box:

- **Upstream:** a mock BMTC (`cmd/mockbmtc`) that the collector polls over
  HTTPS, with BMTC's endpoints, auth and data quirks, and runtime faults.
- **Downstream:** a buyer load client (`internal/load`) that calls the
  Consumer API like many buyer apps and checks every response.
- **Runner:** `cmd/harness` starts the local stack, runs scenarios and writes
  reports.

Everything runs locally. The collector's only feed is the mock (`harness run`
refuses to start otherwise). No dev or production URLs, keys or tokens are
used.

## Requirements

- Docker Desktop with at least 6 CPUs and 12 GB of memory for Docker.
- Go 1.27.
- `transit-hub-collector` and `transit-hub-consumer` checked out next to this
  repository (or pass `COLLECTOR=` / `CONSUMER=`).

## Usage

```bash
make up                 # build both services from their Dockerfiles and start the stack
make smoke              # TC-6 smoke run, about 4 minutes
make run SCENARIO=scenarios/tc-06-endpoint-baseline.yaml   # TC-6, about 55 minutes
make status
make down               # stop and wipe the test data
```

Reports go to `reports/<scenario>-<time>/report.md` and `report.json`: pass or
fail for each threshold, per-phase latency percentiles, status codes, check
failures, harness saturation, and what Prometheus saw (projector lag,
collection success, server-side 5xx and p95, memory, goroutines, alerts).
`harness run` exits 0 on pass, 3 on fail.

While the stack is up:

| Service | URL |
|---|---|
| Consumer API | http://127.0.0.1:18082 |
| Collector metrics | http://127.0.0.1:18080/metrics |
| Prometheus | http://127.0.0.1:19090 |
| Grafana (collector dashboard) | http://127.0.0.1:13000 |
| Mock BMTC control API | http://127.0.0.1:19091 |
| Toxiproxy API | http://127.0.0.1:18474 |

## Stack

`deploy/compose.yaml`:

- **postgres:** write_db and read_db. Every service reaches it through
  **toxiproxy** (`postgres` proxy), so scenarios can add latency or cut it.
- **mockbmtc:** feed on `:9443` (HTTPS with a harness CA in `.harness/tls`;
  the collector trusts it through `SSL_CERT_FILE`), control API on `:9091`.
- **collector** and **consumer:** built from the sibling checkouts' own
  Dockerfiles (the release images), with CPU and memory limits.
- **prometheus:** scrapes both services and the mock every 5 s, with both
  repositories' alert rules loaded.
- **grafana:** the collector's dashboard.

`.harness/` (gitignored) holds the generated admin key, mock token and TLS
files. None of them are real secrets.

## Scenarios

A scenario (`scenarios/*.yaml`) sets the upstream payload size, the collector
interval, the buyer identities, the traffic phases, an optional
`fault_schedule`, and `acceptance_thresholds`. See
`internal/scenario/scenario.go` for every field.

Identity kinds:

| Kind | Created by | Must get |
|---|---|---|
| `valid` | admin API, unlimited quota | 200 / 304 |
| `low_quota` | admin API, `low_quota_per_min` | 200 / 304, then 429 with Retry-After |
| `malformed` | random key, never registered | 403 |
| `unknown_agency` | admin API, used against a missing agency | 404 |

Checks on every response: status for the identity kind; ETag; 304 without a
body; static zip valid with the GTFS files; real-time protobuf valid, every
vehicle with `vehicle.vehicle.id` and none twice; `X-Hub-Static-Version`
matching the active static version; live-data age.

## Mock BMTC control API

```bash
# 30 % of vehicle calls answer 503; zone 3 drops the connection
curl -X PUT http://127.0.0.1:19091/_harness/faults -d '[
  {"endpoint":"vehicles","action":"status","status":503,"rate":0.3},
  {"endpoint":"trips","zone":3,"action":"drop"}]'
curl -X DELETE http://127.0.0.1:19091/_harness/faults
curl -X POST http://127.0.0.1:19091/_harness/static/bump   # publish a new schedule
curl http://127.0.0.1:19091/_harness/stats
```

Actions: `status` (4xx/5xx), `delay`, `trickle` (slow body), `drop`,
`malformed` (`truncate`, `garbage`, `empty`), `auth` (BMTC's 401). Each rule
can set `rate` (share of requests) and `count` (stop after n hits).

## Limits

- Results are local: comparable between runs on the same machine, not
  production capacity figures.
- S3 and OpenSearch are not yet part of the system, so they are not tested.
- M1 supports one agency. The 25-agency ramp (TC-2) and the inactive,
  revoked and ungranted identities (TC-9) need the Phase 1 changes in plan
  section 5.

## Running in gtfs-poc (server)

The harness ships inside gtfs-poc as two compose services behind the
`harness` profile (see the repo-root `docker-compose.yml`):

| Service | What | Lifetime |
|---|---|---|
| `harness-mock` | Mock BMTC, plain HTTP on `:9080`, control API on `:9091` | Long-running (`restart: unless-stopped`) |
| `harness` | Runner (`/harness`) | One-shot: `docker compose --profile harness run --rm harness …` |

Path from the dev collector to the mock:

```
dev collector ─HTTPS─► fis-staging.ondc.org/gtfs/mock-agency/…   (host nginx, TLS)
              ─HTTP──► web:80 /mock-agency/…                      (gtfs-poc nginx)
              ─HTTP──► harness-mock:9080 /…                       (mock feed)
```

```bash
docker compose --profile harness up -d --build harness-mock
curl -s https://fis-staging.ondc.org/gtfs/mock-agency/_ping      # "mock-agency ok"
curl -s http://127.0.0.1:19191/_harness/stats                    # control API, on the server only
docker compose --profile harness stop harness-mock
```

When `harness-mock` isn't running, `/gtfs/mock-agency/` answers 502 and the
demo is unaffected.

### Named mock agencies (G2)

The mock serves any number of named agencies under `/{agency_id}/…`, each
with its own token, data settings, faults, static version and stats:

```bash
C=http://127.0.0.1:19191
curl -X PUT $C/_harness/agencies/MOCK_01 -d '{"token":"tha_…","vehicles":7000,"shift_s":19800}'
curl $C/_harness/agencies                          # lists them, never their tokens
curl -X PUT "$C/_harness/faults?agency=MOCK_01" -d '[{"endpoint":"vehicles","action":"status","status":503}]'
curl "$C/_harness/stats?agency=MOCK_01"
curl -X DELETE $C/_harness/agencies/MOCK_01
```

An unknown agency gets BMTC's "Invalid Token" 401, so the feed doesn't
reveal which IDs exist. Agencies and their tokens are kept in
`/state/agencies.json` (mode 0600, `harness-state` volume), so a mock restart
keeps serving them. The root paths are still the default agency
(`HARNESS_MOCK_TOKEN`), as the local stack uses.

### Running against a deployed Transit Hub (G3)

```bash
# in the server's .env: HARNESS_ADMIN_API_KEY=<target consumer admin key>
docker compose --profile harness up -d --build harness-mock
docker compose --profile harness run --rm harness run --target dev /scenarios/dev/tc-06-smoke.yaml
docker compose --profile harness run --rm harness cleanup        # only after a killed run
```

A dev run, entirely through public APIs:
1. **Preflight:** the target's `/readyz`, its agency API (admin key valid, managed prefix `MOCK_`) and the mock.
2. **Agencies:** `POST /v1/admin/agencies` for `MOCK_01…MOCK_nn` with `feed_base_url = HARNESS_MOCK_PUBLIC_URL/MOCK_nn`, the scenario's interval and retention, and `vehicle_ts_offset_s: 19800`.
3. **Tokens:** each returned `auth_token` is registered with the mock (`PUT /_harness/agencies/MOCK_nn`), then polling is started (`PUT …/polling`).
4. **Buyers:** created with grants to the mock agencies only. The run waits until a buyer gets real-time data and a static zip.
5. **Load:** the scenario's phases and checks run against `MOCK_01`; fault steps apply to every mock agency.
6. **Report:** written to `/reports` (`harness-reports` volume).
7. **Cleanup** (always, also on Ctrl-C):
   - polling is stopped;
   - after one collector reload (`HARNESS_CLEANUP_WAIT`, default 35 s), the agencies are purged on the target, which also removes the buyers' grants;
   - the mock forgets them.

**Safety:**
- **Targets:** only `gtfs-dev.ondc.tech` unless `HARNESS_ALLOWED_HOSTS` adds a host.
- **Agencies:** only `MOCK_` agencies are created or touched.
- **Secrets:** the admin key comes from the environment only, and tokens are never logged or written to reports.

**Dev scenarios** (`scenarios/dev/`, short, low-rate):

| Scenario | Covers |
|---|---|
| `tc-01-short.yaml` | TC-1: 7,000-vehicle feed every 10 s, read back and checked |
| `tc-02-ramp-short.yaml` | TC-2: three agencies collected in parallel |
| `tc-05-upstream-faults-short.yaml` | TC-5 agency side: 503s, then timeouts, then recovery |
| `tc-06-smoke.yaml` | TC-6: every endpoint and access check at low rate |

**Not yet:**
- server-side measurements: projector lag, collection success, alerts (G4);
- auto-stop and the host-load guard (G5);
- deactivating the run's buyers, which needs the consumer's profile admin API (A2). Until then they stay active, but with no grants left;
- the fault adapter (G9).
