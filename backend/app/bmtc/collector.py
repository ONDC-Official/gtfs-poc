"""BMTC collector: live vehicles every 10s, trip updates every 60s, static
GTFS every 72h, and 72h retention - all into the `bmtc` Postgres schema.

    python -m app.bmtc.collector

Feed quirks handled here (see docs on the BMTC API test findings):
  * POST with `authToken` + `Content-Type: application/json` (415 without it).
  * Success wraps data in {Data, Message, IsSuccess, Exception, RowCount};
    errors use {status, errorMessage}. RowCount is always 0 - never used.
  * Vehicle timestamps are IST written as UTC epoch (+19800s).
  * entity.id is not unique; vehicle.id is the key. Repeats of an already
    stored (vehicle_id, ts) are skipped by the primary key.
  * Static files arrive as arrays of CSV lines; agency.txt's records are
    concatenated into one line; calendar dates are '000000'.
"""
import csv

import logging
import re
import signal
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ..config import settings
from ..data import pg

log = logging.getLogger("gtfs.bmtc")

_SCHEMA = Path(__file__).resolve().parents[1] / "data" / "schema_bmtc.sql"
_HOUR = timedelta(hours=1)
# Hourly partitions this process has created or found; see store_vehicles.
_known_hours: set = set()

VEHICLE_EP = "GetGTFSVehicleUpdateData"
TRIP_EP = "GetGTFSTripsUpdateData"
STATIC_EP = "GetGTFSStaticData"

# check number -> (file name, target table, {column: converter})
STATIC_FILES: Dict[int, Tuple[str, str, Dict[str, Any]]] = {}


# ---------------------------------------------------------------------------
# Value cleaning
# ---------------------------------------------------------------------------

def _s(v: Any) -> Optional[str]:
    """Empty / placeholder strings -> None."""
    if v is None:
        return None
    v = str(v).strip()
    return None if v in ("", "N/A") else v


def _int(v: Any) -> Optional[int]:
    v = _s(v)
    try:
        return int(float(v)) if v is not None else None
    except ValueError:
        return None


def _float(v: Any) -> Optional[float]:
    v = _s(v)
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


def _bool01(v: Any) -> Optional[bool]:
    v = _s(v)
    return None if v is None else v == "1"


def _yyyymmdd(v: Any) -> Optional[date]:
    """'20261018' -> date; '000000', '' or anything malformed -> None."""
    v = _s(v)
    if not v or not re.fullmatch(r"\d{8}", v):
        return None
    try:
        return datetime.strptime(v, "%Y%m%d").date()
    except ValueError:
        return None


def _epoch(v: Any, offset_s: int = 0) -> Optional[datetime]:
    n = _int(v)
    if n is None or n <= 0:
        return None
    return datetime.fromtimestamp(n - offset_s, tz=timezone.utc)


STATIC_FILES.update({
    0: ("agency", "agency", {"agency_id": _s, "agency_name": _s, "agency_url": _s,
                             "agency_timezone": _s, "agency_phone": _s,
                             "agency_lang": _s}),
    1: ("stops", "stops", {"stop_id": _s, "stop_name": _s,
                           "stop_lat": _float, "stop_lon": _float}),
    2: ("routes", "routes", {"route_id": _s, "agency_id": _s,
                             "route_short_name": _s, "route_long_name": _s,
                             "route_desc": _s, "route_type": _int}),
    3: ("trips", "trips", {"trip_id": _s, "route_id": _s, "service_id": _s}),
    4: ("stop_times", "stop_times", {"trip_id": _s, "stop_sequence": _int,
                                     "arrival_time": _s, "departure_time": _s,
                                     "stop_id": _s}),
    5: ("calendar", "calendar", {"service_id": _s, "monday": _bool01,
                                 "tuesday": _bool01, "wednesday": _bool01,
                                 "thursday": _bool01, "friday": _bool01,
                                 "saturday": _bool01, "sunday": _bool01,
                                 "start_date": _yyyymmdd, "end_date": _yyyymmdd}),
})
# Primary key per static table, used to drop duplicate records in a pull.
_STATIC_PK = {"agency": ("agency_id",), "stops": ("stop_id",),
              "routes": ("route_id",), "trips": ("trip_id",),
              "stop_times": ("trip_id", "stop_sequence"),
              "calendar": ("service_id",)}


# ---------------------------------------------------------------------------
# Parsing (pure; unit-tested)
# ---------------------------------------------------------------------------

def parse_envelope(body: Any) -> Tuple[bool, Any, Optional[str]]:
    """-> (ok, Data, error). Accepts both response shapes BMTC uses."""
    if not isinstance(body, dict):
        return False, None, "response is not a JSON object"
    if "IsSuccess" in body:
        if body.get("IsSuccess") is True:
            return True, body.get("Data"), None
        return False, None, str(body.get("Exception") or body.get("Message")
                                or "IsSuccess=false")
    if body.get("status") == "Error" or "errorMessage" in body:
        return False, None, str(body.get("errorMessage") or "error")
    return False, None, "unrecognised response shape"


def parse_vehicles(data: Any, now: datetime, offset_s: int,
                   retention_s: int) -> Tuple[List[Dict[str, Any]], Optional[datetime], int]:
    """Feed Data -> (rows, header_ts, stale).

    One row per vehicle (newest wins within a response). Positions older than
    the retention window are counted as stale and not returned - retention
    would delete them on the next pass anyway, and they would otherwise force
    partitions for hours long gone."""
    data = data if isinstance(data, dict) else {}
    header_ts = _epoch((data.get("header") or {}).get("timestamp"))
    cutoff = now.timestamp() - retention_s
    by_vehicle: Dict[str, Dict[str, Any]] = {}
    stale = 0
    for ent in data.get("entity") or []:
        v = (ent or {}).get("vehicle") or {}
        vid = _s((v.get("vehicle") or {}).get("id"))
        pos = v.get("position") or {}
        ts = _epoch(v.get("timestamp"), offset_s)
        lat, lon = _float(pos.get("latitude")), _float(pos.get("longitude"))
        if vid is None or ts is None or lat is None or lon is None:
            continue
        if ts.timestamp() < cutoff:
            stale += 1
            continue
        trip = v.get("trip") or {}
        bearing = _float(pos.get("bearing"))
        sr = _s(trip.get("schedule_relationship"))
        row = {
            "vehicle_id": vid,
            "label": _s((v.get("vehicle") or {}).get("label")),
            "ts": ts,
            "lat": lat,
            "lon": lon,
            "bearing": None if not bearing else bearing,
            "trip_id": _s(trip.get("trip_id")),
            "route_id": _s(trip.get("route_id")),
            "start_time": _s(trip.get("start_time")),
            "start_date": _yyyymmdd(trip.get("start_date")),
            "scheduled": None if sr is None else sr == "SCHEDULED",
            "sch_no": _s(trip.get("sch_no")),
            "stop_id": _s(v.get("stop_id")),
            "status": _int(v.get("current_status")),
        }
        prev = by_vehicle.get(vid)
        if prev is None or row["ts"] > prev["ts"]:
            by_vehicle[vid] = row
    return list(by_vehicle.values()), header_ts, stale


_AGENCY_RECORD = re.compile(r"(?<![\d,])(?=\d+,[^,]*,https?://)")


def parse_static_csv(check: int, lines: Any) -> Tuple[List[str], List[Dict[str, Any]], List[str]]:
    """Array of CSV lines -> (header, cleaned rows, warnings).

    Rows are keyed by the target table's columns; columns the API adds that
    the table does not know are reported in warnings rather than lost silently."""
    _, table, conv = STATIC_FILES[check]
    warnings: List[str] = []
    if not isinstance(lines, list) or not lines:
        return [], [], ["no data"]
    header = next(csv.reader([str(lines[0])]))
    body = [str(x) for x in lines[1:] if str(x).strip()]
    if check == 0 and body:
        # agency.txt arrives as one concatenated line; split before each
        # `<id>,<name>,http(s)://` that does not follow a digit or comma.
        joined = "".join(body)
        split = [p for p in _AGENCY_RECORD.split(joined) if p]
        if len(split) > len(body):
            warnings.append(f"agency records re-split: {len(body)} line(s) -> {len(split)}")
            body = split
    unknown = [h for h in header if h not in conv]
    if unknown:
        warnings.append("columns not stored: " + ",".join(unknown))
    rows, bad = [], 0
    seen = set()
    pk = _STATIC_PK[table]
    for rec in csv.reader(body):
        if len(rec) != len(header):
            bad += 1
            continue
        raw = dict(zip(header, rec))
        row = {col: fn(raw.get(col)) for col, fn in conv.items()}
        key = tuple(row[k] for k in pk)
        if any(k is None for k in key) or key in seen:
            bad += 1
            continue
        seen.add(key)
        rows.append(row)
    if bad:
        warnings.append(f"{bad} malformed or duplicate row(s) skipped")
    return header, rows, warnings


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

# Session limits on every collector connection. Analysis queries on the same
# tables must never stall collection: a write that cannot get its lock in 5s
# fails and is retried on the next tick instead of queueing (and, while
# queued, blocking everyone behind it).
_SESSION_LIMITS = "SET lock_timeout = '5s'; SET statement_timeout = '90s'"


@contextmanager
def _conn():
    # Re-applied on every checkout (one cheap round trip): the pool may hand
    # out a fresh connection at any time, and init_schema resets its own.
    with pg.pool().connection() as conn:
        conn.execute(_SESSION_LIMITS)
        yield conn


# Both BMTC collectors apply the schema at start; the advisory lock keeps two
# containers starting together from racing on CREATE OR REPLACE FUNCTION.
_SCHEMA_LOCK = 0x626d7463  # 'bmtc'


def init_schema() -> None:
    with pg.pool().connection() as conn:
        conn.execute("SET lock_timeout = '60s'")
        conn.execute("SELECT pg_advisory_lock(%s)", (_SCHEMA_LOCK,))
        try:
            conn.execute(_SCHEMA.read_text())
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_SCHEMA_LOCK,))
            conn.execute("RESET lock_timeout")
    log.info("bmtc schema ready")


def log_poll(endpoint: str, **f: Any) -> int:
    cols = ["endpoint"] + list(f)
    with _conn() as conn:
        return conn.execute(
            f"INSERT INTO bmtc.poll_log ({','.join(cols)}) "
            f"VALUES ({','.join(['%s'] * len(cols))}) RETURNING poll_id",
            [endpoint, *f.values()]).fetchone()["poll_id"]


_VP_COLS = ["vehicle_id", "ts", "lat", "lon", "bearing", "trip_id", "route_id",
            "start_time", "start_date", "scheduled", "sch_no", "stop_id",
            "status", "poll_id", "ingested_at"]
_VP_TYPES = ["text", "timestamptz", "float8", "float8", "real", "text", "text",
             "text", "date", "boolean", "text", "text", "smallint", "bigint",
             "timestamptz"]


def _arrays(rows: List[Dict[str, Any]], cols: List[str]) -> List[List[Any]]:
    return [[r[c] for r in rows] for c in cols]


def store_vehicles(rows: List[Dict[str, Any]], poll_id: int, now: datetime) -> int:
    """Insert new positions, refresh latest + vehicles. Returns new row count."""
    if not rows:
        return 0
    for r in rows:
        r["poll_id"], r["ingested_at"] = poll_id, now
    hours = {r["ts"].replace(minute=0, second=0, microsecond=0) for r in rows
             if r["ts"] <= now.replace(microsecond=0) + _HOUR}
    # Creating a partition locks the parent table, so only attempt hours not
    # already seen; in steady state that is one attempt per new hour, not one
    # per hour-in-batch every 10s (which queued behind long analysis reads).
    missing = hours - _known_hours
    unnest = ", ".join(f"%s::{t}[]" for t in _VP_TYPES)
    with _conn() as conn:
        with conn.transaction():
            for h in sorted(missing):
                conn.execute("SELECT bmtc.ensure_hour_partition(%s)", (h,))
            new = conn.execute(
                f"INSERT INTO bmtc.vehicle_position ({','.join(_VP_COLS)}) "
                f"SELECT * FROM unnest({unnest}) "
                "ON CONFLICT (vehicle_id, ts) DO NOTHING",
                _arrays(rows, _VP_COLS)).rowcount
            lcols = ["vehicle_id", "ts", "lat", "lon", "bearing", "trip_id",
                     "route_id", "sch_no", "stop_id", "status", "ingested_at"]
            ltypes = ["text", "timestamptz", "float8", "float8", "real", "text",
                      "text", "text", "text", "smallint", "timestamptz"]
            upd = ", ".join(f"{c}=EXCLUDED.{c}" for c in lcols[1:])
            conn.execute(
                f"INSERT INTO bmtc.vehicle_latest ({','.join(lcols)}) "
                f"SELECT * FROM unnest({', '.join(f'%s::{t}[]' for t in ltypes)}) "
                f"ON CONFLICT (vehicle_id) DO UPDATE SET {upd} "
                "WHERE EXCLUDED.ts >= bmtc.vehicle_latest.ts",
                _arrays(rows, lcols))
            conn.execute(
                "INSERT INTO bmtc.vehicles (vehicle_id, label, first_seen, last_seen) "
                "SELECT v, l, %s, %s FROM unnest(%s::text[], %s::text[]) AS u(v, l) "
                "ON CONFLICT (vehicle_id) DO UPDATE SET "
                "label = COALESCE(EXCLUDED.label, bmtc.vehicles.label), "
                "last_seen = EXCLUDED.last_seen",
                (now, now, [r["vehicle_id"] for r in rows], [r["label"] for r in rows]))
    # Only after commit: a rolled-back CREATE must be retried next tick.
    _known_hours.update(missing)
    return new


def replace_static(table: str, rows: List[Dict[str, Any]]) -> None:
    cols = list(rows[0])
    with _conn() as conn:
        with conn.transaction():
            conn.execute(f"TRUNCATE bmtc.{table}")
            with conn.cursor().copy(
                    f"COPY bmtc.{table} ({','.join(cols)}) FROM STDIN") as cp:
                for r in rows:
                    cp.write_row([r[c] for c in cols])


def last_static_pull() -> Optional[datetime]:
    """Time of the last pull in which every file that returned rows loaded."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT max(pulled_at) AS t FROM bmtc.static_pull_log "
            "WHERE check_no = 3 AND row_count > 0").fetchone()
    return row["t"] if row else None


def apply_retention(now: datetime) -> Dict[str, int]:
    cutoff = now.timestamp() - settings.bmtc_retention_hours * 3600
    cut = datetime.fromtimestamp(cutoff, tz=timezone.utc)
    out = {}
    with _conn() as conn:
        with conn.transaction():
            out["partitions_dropped"] = conn.execute(
                "SELECT bmtc.drop_old_partitions(%s) AS n", (cut,)).fetchone()["n"]
            # Hourly partitions leave at most the boundary hour; the default
            # partition holds only rows written before their hour existed.
            out["boundary_rows"] = conn.execute(
                "DELETE FROM bmtc.vehicle_position WHERE ts < %s", (cut,)).rowcount
            for t, col in (("poll_log", "polled_at"), ("trip_update", "update_ts"),
                           ("static_pull_log", "pulled_at"),
                           ("vehicle_latest", "ts"), ("vehicles", "last_seen")):
                if t == "static_pull_log":
                    # Keep the latest successful pull's log so the 72h timer
                    # survives retention.
                    out[t] = conn.execute(
                        "DELETE FROM bmtc.static_pull_log WHERE pulled_at < %s "
                        "AND pulled_at < (SELECT coalesce(max(pulled_at), %s) "
                        "FROM bmtc.static_pull_log WHERE check_no = 3 AND row_count > 0)",
                        (cut, cut)).rowcount
                else:
                    out[t] = conn.execute(
                        f"DELETE FROM bmtc.{t} WHERE {col} < %s", (cut,)).rowcount
    return out


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------

class BmtcClient:
    """POSTs to BMTC's GTFSDataFeed with the auth headers it requires.
    Shared by the vehicle collector and the trip collector (trips.py)."""

    def __init__(self) -> None:
        if not settings.bmtc_auth_token:
            raise SystemExit("BMTC_AUTH_TOKEN is not set")
        self.http = httpx.Client(
            base_url=settings.bmtc_base_url.rstrip("/") + "/",
            timeout=settings.bmtc_timeout_seconds,
            headers={"authToken": settings.bmtc_auth_token,
                     "Content-Type": "application/json"})

    def post(self, ep: str, body: Dict[str, Any], timeout: Optional[float] = None):
        """-> (http_status, ok, Data, error, latency_ms)."""
        t0 = time.perf_counter()
        try:
            r = self.http.post(ep, json=body, timeout=timeout or settings.bmtc_timeout_seconds)
        except httpx.HTTPError as exc:
            return None, False, None, f"{type(exc).__name__}: {exc}"[:500], \
                int((time.perf_counter() - t0) * 1000)
        ms = int((time.perf_counter() - t0) * 1000)
        try:
            body_json = r.json()
        except ValueError:
            return r.status_code, False, None, f"non-JSON response ({len(r.content)} B)", ms
        ok, data, err = parse_envelope(body_json)
        return r.status_code, ok, data, err, ms

    def close(self) -> None:
        self.http.close()


class Collector:
    """Vehicle positions every 10s, static files 0-3 and 5 every 72h, and
    retention for the tables it writes. Trip updates and stop_times (check 4,
    zone-wise) belong to the trip collector."""

    def __init__(self) -> None:
        self.client = BmtcClient()
        self.stopping = False
        self.next_retention = 0.0
        self.next_static_check = 0.0

    def poll_vehicles(self) -> None:
        now = datetime.now(timezone.utc)
        status, ok, data, err, ms = self.client.post(VEHICLE_EP, {})
        rows, header_ts, stale = ([], None, 0)
        if ok:
            rows, header_ts, stale = parse_vehicles(
                data, now, settings.bmtc_ts_offset_s, settings.bmtc_retention_hours * 3600)
        entities = len((data or {}).get("entity") or []) if ok else None
        poll_id = log_poll("vehicle", http_status=status, is_success=ok, error=err,
                           header_ts=header_ts, entities=entities, stale=stale,
                           latency_ms=ms)
        new = 0
        if rows:
            try:
                new = store_vehicles(rows, poll_id, now)
            except Exception as exc:
                log.exception("store failed")
                err = f"store failed: {exc}"[:500]
        with _conn() as conn:
            conn.execute("UPDATE bmtc.poll_log SET new_rows=%s, duplicates=%s, "
                         "error=coalesce(%s, error) WHERE poll_id=%s",
                         (new, len(rows) - new, err if rows else None, poll_id))
        log.info("vehicles | http=%s ok=%s entities=%s new=%s dup=%s stale=%s %sms%s",
                 status, ok, entities, new, len(rows) - new, stale, ms,
                 f" | {err}" if err else "")

    def pull_static(self) -> bool:
        """Fetch checks 0-3 and 5. A file that fails or comes back empty keeps
        its previous contents. Returns True when trips loaded (the timer
        anchor). Check 4 (stop_times) is zone-wise and pulled by trips.py."""
        trips_ok = False
        for check, (name, table, _) in STATIC_FILES.items():
            if check == 4:
                continue
            status, ok, data, err, _ms = self.client.post(STATIC_EP, {"check": check}, timeout=60)
            n, warns = 0, [err] if err else []
            if ok:
                _, rows, w = parse_static_csv(check, data)
                warns += w
                if rows:
                    try:
                        replace_static(table, rows)
                        n = len(rows)
                    except Exception as exc:
                        log.exception("static %s load failed", name)
                        warns.append(f"load failed: {exc}"[:300])
                else:
                    warns.append("empty - previous contents kept")
            with _conn() as conn:
                conn.execute(
                    "INSERT INTO bmtc.static_pull_log (check_no, file_name, http_status, "
                    "row_count, warnings) VALUES (%s,%s,%s,%s,%s)",
                    (check, name, status, n, "; ".join(warns) or None))
            log.info("static %s | http=%s rows=%s%s", name, status, n,
                     f" | {'; '.join(warns)}" if warns else "")
            trips_ok = trips_ok or (check == 3 and n > 0)
        return trips_ok

    def _maybe_static(self, mono: float) -> None:
        if mono < self.next_static_check:
            return
        last = last_static_pull()
        due = last is None or (datetime.now(timezone.utc) - last).total_seconds() \
            >= settings.bmtc_static_every_hours * 3600
        if due:
            ok = self.pull_static()
            # Failed pull: retry in an hour. Success: re-check hourly anyway.
            log.info("static pull %s", "complete" if ok else "failed - retrying in 1h")
        self.next_static_check = mono + 3600

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)
        init_schema()
        log.info("bmtc collector started | vehicles every %ss, "
                 "static every %sh, retention %sh",
                 settings.bmtc_poll_seconds,
                 settings.bmtc_static_every_hours, settings.bmtc_retention_hours)
        period = settings.bmtc_poll_seconds
        next_tick = time.monotonic()
        while not self.stopping:
            mono = time.monotonic()
            for name, job in (("vehicles", self.poll_vehicles),
                              ("side jobs", lambda: self._side_jobs(mono))):
                try:
                    job()
                except Exception:
                    log.exception("%s failed", name)
            # Fixed cadence: ticks stay on a 10s grid; an overrun skips ticks
            # rather than firing back-to-back.
            next_tick += period
            now = time.monotonic()
            if next_tick < now:
                skipped = int((now - next_tick) // period) + 1
                log.warning("tick overran - skipping %s tick(s)", skipped)
                next_tick += skipped * period
            while not self.stopping and time.monotonic() < next_tick:
                time.sleep(min(0.5, next_tick - time.monotonic()))
        self.client.close()
        pg.close()
        log.info("bmtc collector stopped")

    def _side_jobs(self, mono: float) -> None:
        self._maybe_static(mono)
        if mono >= self.next_retention:
            self.next_retention = mono + 3600
            log.info("retention | %s", apply_retention(datetime.now(timezone.utc)))

    def _stop(self, *_: Any) -> None:
        self.stopping = True


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
                        datefmt="%H:%M:%S")
    # httpx logs every request at INFO; poll lines already carry the outcome.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    Collector().run()


if __name__ == "__main__":
    main()
