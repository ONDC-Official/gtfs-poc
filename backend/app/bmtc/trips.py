"""BMTC trip collector: trip updates every 15 min and stop_times every 72h,
both zone-wise (zoneid 1-7), into bmtc.trip_status / bmtc.stop_delay /
bmtc.stop_times.

    python -m app.bmtc.trips

Runs in its own container next to the vehicle collector (collector.py): a
full trip round is ~256 MB over ~5 min, which must never delay the 10s
vehicle polls.

What the feed sends (zone test, 19 Oct 2026): every trip of the service day,
00:00-23:55, each with *all* its stops including ones already passed, and
per-stop arrival/departure delay in seconds. No per-trip timestamp, no
vehicle id, start_date as DDMMYYYY. So each round upserts the latest state;
a stop's final value is the delay the bus actually had there, and only rows
whose values changed are written.
"""
import logging
import shutil
import signal
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from ..config import settings
from ..data import pg
from .collector import (STATIC_EP, TRIP_EP, BmtcClient, _conn, _epoch, _int, _s,
                        init_schema, log_poll, parse_static_csv)

log = logging.getLogger("gtfs.bmtc.trips")

IST = ZoneInfo("Asia/Kolkata")
IMPLAUSIBLE_S = 7200          # |delay| above this is flagged, not dropped


# ---------------------------------------------------------------------------
# Parsing (pure; unit-tested)
# ---------------------------------------------------------------------------

def parse_service_date(v: Any) -> Optional[date]:
    """'19102026' (DDMMYYYY, this feed) or '20261019' (YYYYMMDD, the vehicle
    feed) -> date. Whichever reading yields a plausible year wins."""
    v = _s(v)
    if not v or len(v) != 8 or not v.isdigit():
        return None
    for fmt in ("%d%m%Y", "%Y%m%d"):
        try:
            d = datetime.strptime(v, fmt).date()
        except ValueError:
            continue
        if 2000 <= d.year <= 2100:
            return d
    return None


def parse_trip_feed(data: Any, zone: int) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Feed Data -> (trip rows, stop rows), deduplicated on their keys."""
    data = data if isinstance(data, dict) else {}
    trips: Dict[Tuple, Dict[str, Any]] = {}
    stops: Dict[Tuple, Dict[str, Any]] = {}
    for ent in data.get("entity") or []:
        tu = (ent or {}).get("trip_update") or {}
        t = tu.get("trip") or {}
        trip_id = _s(t.get("trip_id"))
        day = parse_service_date(t.get("start_date"))
        if trip_id is None or day is None:
            continue
        total = skipped = with_delay = 0
        max_delay: Optional[int] = None
        for u in tu.get("stop_time_update") or []:
            seq = _int((u or {}).get("stop_sequence"))
            if seq is None:
                continue
            arr = _int((u.get("arrival") or {}).get("delay"))
            dep = _int((u.get("departure") or {}).get("delay"))
            st = _s(u.get("schedule_relationship"))
            implausible = any(d is not None and abs(d) > IMPLAUSIBLE_S for d in (arr, dep))
            stops[(day, trip_id, seq)] = {
                "service_date": day, "trip_id": trip_id, "stop_sequence": seq,
                "stop_id": _s(u.get("stop_id")), "arrival_delay_s": arr,
                "departure_delay_s": dep, "stop_status": st,
                "implausible": implausible}
            total += 1
            skipped += st == "SKIPPED"
            if arr is not None:
                with_delay += 1
                if not implausible and (max_delay is None or arr > max_delay):
                    max_delay = arr
        trips[(day, trip_id)] = {
            "service_date": day, "trip_id": trip_id, "zone_id": zone,
            "route_id": _s(t.get("route_id")), "start_time": _s(t.get("start_time")),
            "status": _s(t.get("schedule_relationship")), "stops_total": total,
            "stops_skipped": skipped, "stops_with_delay": with_delay,
            "max_delay_s": max_delay}
    return list(trips.values()), list(stops.values())


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

_TRIP_COLS = ["service_date", "trip_id", "zone_id", "route_id", "start_time",
              "status", "stops_total", "stops_skipped", "stops_with_delay",
              "max_delay_s"]
_STOP_COLS = ["service_date", "trip_id", "stop_sequence", "stop_id",
              "arrival_delay_s", "departure_delay_s", "stop_status", "implausible"]
_known_days: set = set()


def store_round(trips: List[Dict[str, Any]], stops: List[Dict[str, Any]],
                now: datetime, poll_id: int) -> Tuple[int, int]:
    """Upsert one zone's round. Returns (stop rows inserted, stop rows changed).
    Unchanged stop rows are not touched; trip rows always refresh last_seen."""
    days = {r["service_date"] for r in stops} - _known_days
    with _conn() as conn:
        with conn.transaction():
            conn.execute("SET LOCAL statement_timeout = '300s'")
            for d in sorted(days):
                conn.execute("SELECT bmtc.ensure_day_partition(%s)", (d,))
            # Staging tables carry only the columns copied in - no NOT NULLs
            # (first_seen/last_seen are filled in by the upsert below).
            conn.execute(f"CREATE TEMP TABLE tmp_trip ON COMMIT DROP AS "
                         f"SELECT {','.join(_TRIP_COLS)} FROM bmtc.trip_status WITH NO DATA")
            conn.execute(f"CREATE TEMP TABLE tmp_stop ON COMMIT DROP AS "
                         f"SELECT {','.join(_STOP_COLS)} FROM bmtc.stop_delay WITH NO DATA")
            with conn.cursor().copy(f"COPY tmp_trip ({','.join(_TRIP_COLS)}) FROM STDIN") as cp:
                for r in trips:
                    cp.write_row([r[c] for c in _TRIP_COLS])
            with conn.cursor().copy(f"COPY tmp_stop ({','.join(_STOP_COLS)}) FROM STDIN") as cp:
                for r in stops:
                    cp.write_row([r[c] for c in _STOP_COLS])

            tset = ", ".join(f"{c} = EXCLUDED.{c}" for c in _TRIP_COLS[2:])
            conn.execute(
                f"INSERT INTO bmtc.trip_status ({','.join(_TRIP_COLS)}, first_seen, "
                f"last_seen, canceled_at, poll_id) "
                f"SELECT {','.join(_TRIP_COLS)}, %(now)s, %(now)s, "
                f"CASE WHEN status = 'CANCELED' THEN %(now)s::timestamptz END, %(pid)s "
                f"FROM tmp_trip "
                f"ON CONFLICT (service_date, trip_id) DO UPDATE SET {tset}, "
                f"last_seen = EXCLUDED.last_seen, poll_id = EXCLUDED.poll_id, "
                f"canceled_at = COALESCE(bmtc.trip_status.canceled_at, EXCLUDED.canceled_at)",
                {"now": now, "pid": poll_id})

            vals = ["stop_id", "arrival_delay_s", "departure_delay_s", "stop_status",
                    "implausible"]
            sset = ", ".join(f"{c} = EXCLUDED.{c}" for c in vals)
            old = ", ".join(f"d.{c}" for c in vals)
            new = ", ".join(f"EXCLUDED.{c}" for c in vals)
            # New = staged keys not stored yet; changed = upsert rowcount
            # minus those (unchanged rows are filtered by the WHERE and not
            # written). xmax is unavailable on a partitioned target.
            ins = conn.execute(
                "SELECT count(*) AS n FROM tmp_stop t WHERE NOT EXISTS ("
                " SELECT 1 FROM bmtc.stop_delay d WHERE d.service_date = t.service_date"
                " AND d.trip_id = t.trip_id AND d.stop_sequence = t.stop_sequence)"
            ).fetchone()["n"]
            written = conn.execute(
                f"INSERT INTO bmtc.stop_delay AS d ({','.join(_STOP_COLS)}, first_seen, last_seen)"
                f" SELECT {','.join(_STOP_COLS)}, %(now)s, %(now)s FROM tmp_stop"
                f" ON CONFLICT (service_date, trip_id, stop_sequence) DO UPDATE SET {sset},"
                f" changes = LEAST(d.changes + 1, 32767), last_seen = EXCLUDED.last_seen"
                f" WHERE ({old}) IS DISTINCT FROM ({new})",
                {"now": now}).rowcount
    _known_days.update(days)
    return ins, written - ins


def replace_stop_times(rows: List[Dict[str, Any]]) -> None:
    cols = ["trip_id", "stop_sequence", "arrival_time", "departure_time", "stop_id", "zone_id"]
    with _conn() as conn:
        with conn.transaction():
            conn.execute("SET LOCAL statement_timeout = '600s'")
            conn.execute("TRUNCATE bmtc.stop_times")
            with conn.cursor().copy(
                    f"COPY bmtc.stop_times ({','.join(cols)}) FROM STDIN") as cp:
                for r in rows:
                    cp.write_row([r[c] for c in cols])


def last_stop_times_pull() -> Optional[datetime]:
    # The all-zones marker row (zone_id NULL) is written only after a
    # complete replace.
    with _conn() as conn:
        row = conn.execute(
            "SELECT max(pulled_at) AS t FROM bmtc.static_pull_log "
            "WHERE check_no = 4 AND zone_id IS NULL AND row_count > 0").fetchone()
    return row["t"] if row else None


def apply_retention(now: datetime) -> Dict[str, int]:
    """Drop whole service days older than the retention window (IST days)."""
    cutoff = (now - timedelta(hours=settings.bmtc_retention_hours)).astimezone(IST).date()
    out = {"cutoff_day": str(cutoff), "partitions_dropped": 0}
    with _conn() as conn:
        parts = conn.execute(
            "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
            "WHERE i.inhparent = 'bmtc.stop_delay'::regclass "
            "AND c.relname ~ '^stop_delay_[0-9]{8}$'").fetchall()
        for p in parts:
            day = datetime.strptime(p["relname"][-8:], "%Y%m%d").date()
            if day < cutoff:
                conn.execute(f'DROP TABLE bmtc."{p["relname"]}"')
                _known_days.discard(day)
                out["partitions_dropped"] += 1
        out["stop_delay_default"] = conn.execute(
            "DELETE FROM bmtc.stop_delay_default WHERE service_date < %s", (cutoff,)).rowcount
        out["trip_status"] = conn.execute(
            "DELETE FROM bmtc.trip_status WHERE service_date < %s", (cutoff,)).rowcount
    return out


def free_gb() -> float:
    return shutil.disk_usage("/").free / 1e9


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------

class TripCollector:
    def __init__(self) -> None:
        self.client = BmtcClient()
        self.zones = [int(z) for z in settings.bmtc_zones.split(",") if z.strip()]
        self.stopping = False

    def _post_zone(self, ep: str, body: Dict[str, Any]):
        """One retry after 30s on failure: these endpoints take 7-76s and
        occasionally time out."""
        res = self.client.post(ep, body, timeout=settings.bmtc_trip_timeout_seconds)
        if not res[1] and not self.stopping:
            log.warning("%s %s failed (%s) - retrying in 30s", ep, body, res[3])
            self._sleep(30)
            res = self.client.post(ep, body, timeout=settings.bmtc_trip_timeout_seconds)
        return res

    def trip_round(self) -> None:
        if free_gb() < settings.bmtc_min_free_gb:
            log.warning("skipping trip round: %.1f GB free < %.1f GB",
                        free_gb(), settings.bmtc_min_free_gb)
            return
        t0 = time.monotonic()
        totals = [0, 0, 0]
        for z in self.zones:
            if self.stopping:
                return
            now = datetime.now(timezone.utc)
            status, ok, data, err, ms = self._post_zone(TRIP_EP, {"zoneid": z})
            trips, stops = parse_trip_feed(data, z) if ok else ([], [])
            header_ts = _epoch(((data or {}).get("header") or {}).get("timestamp")) if ok else None
            data = None                                 # free the parsed JSON
            poll_id = log_poll("trip_update", zone_id=z, http_status=status,
                               is_success=ok, error=err, entities=len(trips) if ok else None,
                               latency_ms=ms, header_ts=header_ts)
            ins = upd = 0
            if trips:
                try:
                    ins, upd = store_round(trips, stops, now, poll_id)
                except Exception as exc:
                    log.exception("zone %s store failed", z)
                    err = f"store failed: {exc}"[:500]
            with _conn() as conn:
                conn.execute("UPDATE bmtc.poll_log SET new_rows=%s, changed_rows=%s, "
                             "error=coalesce(%s, error) WHERE poll_id=%s",
                             (ins, upd, err if trips else None, poll_id))
            log.info("trips zone %s | http=%s ok=%s trips=%s stops=%s new=%s changed=%s %sms%s",
                     z, status, ok, len(trips), len(stops), ins, upd, ms,
                     f" | {err}" if err else "")
            totals[0] += len(trips); totals[1] += ins; totals[2] += upd
            trips = stops = None
        log.info("trip round done in %.0fs | trips=%s new stop rows=%s changed=%s",
                 time.monotonic() - t0, *totals)

    def pull_stop_times(self) -> bool:
        """All zones or nothing: the table is replaced only when every zone
        returned rows, so a partial pull never leaves a partial timetable."""
        all_rows: List[Dict[str, Any]] = []
        complete = True
        for z in self.zones:
            if self.stopping:
                return False
            status, ok, data, err, ms = self._post_zone(STATIC_EP, {"check": 4, "zoneid": z})
            n, warns = 0, [err] if err else []
            if ok:
                _, rows, w = parse_static_csv(4, data)
                warns += w
                for r in rows:
                    r["zone_id"] = z
                all_rows += rows
                n = len(rows)
            data = None
            complete = complete and n > 0
            with _conn() as conn:
                conn.execute(
                    "INSERT INTO bmtc.static_pull_log (check_no, zone_id, file_name, "
                    "http_status, row_count, warnings) VALUES (4,%s,'stop_times',%s,%s,%s)",
                    (z, status, n, "; ".join(warns) or None))
            log.info("stop_times zone %s | http=%s rows=%s %sms%s", z, status, n, ms,
                     f" | {'; '.join(warns)}" if warns else "")
        if not complete:
            log.warning("stop_times incomplete - previous timetable kept")
            return False
        # Trips never span zones in this feed; dedupe anyway so one stray
        # duplicate cannot fail the whole COPY on the primary key.
        uniq = {(r["trip_id"], r["stop_sequence"]): r for r in all_rows}
        replace_stop_times(list(uniq.values()))
        with _conn() as conn:
            conn.execute(
                "INSERT INTO bmtc.static_pull_log (check_no, zone_id, file_name, row_count, "
                "warnings) VALUES (4, NULL, 'stop_times (all zones)', %s, %s)",
                (len(uniq), None if len(uniq) == len(all_rows)
                 else f"{len(all_rows) - len(uniq)} duplicate rows across zones dropped"))
        log.info("stop_times replaced | %s rows across %s zones", len(uniq), len(self.zones))
        return True

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self.stopping and time.monotonic() < end:
            time.sleep(min(1.0, end - time.monotonic()))

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)
        init_schema()
        period = settings.bmtc_trip_poll_minutes * 60
        log.info("bmtc trip collector started | zones %s, trip updates every %s min, "
                 "stop_times every %sh, retention %sh, min free %.0f GB",
                 self.zones, settings.bmtc_trip_poll_minutes,
                 settings.bmtc_stop_times_every_hours, settings.bmtc_retention_hours,
                 settings.bmtc_min_free_gb)
        next_round = time.monotonic()
        next_hourly = 0.0
        while not self.stopping:
            mono = time.monotonic()
            if mono >= next_hourly:
                next_hourly = mono + 3600
                for name, job in (("stop_times", self._maybe_stop_times),
                                  ("retention", self._retention)):
                    try:
                        job()
                    except Exception:
                        log.exception("%s failed", name)
            if time.monotonic() >= next_round:
                try:
                    self.trip_round()
                except Exception:
                    log.exception("trip round failed")
                next_round += period
                if next_round < time.monotonic():
                    log.warning("trip round overran its %s min slot", settings.bmtc_trip_poll_minutes)
                    next_round = time.monotonic() + period
            self._sleep(min(5.0, max(0.0, next_round - time.monotonic())))
        self.client.close()
        pg.close()
        log.info("bmtc trip collector stopped")

    def _maybe_stop_times(self) -> None:
        last = last_stop_times_pull()
        if last is None or (datetime.now(timezone.utc) - last).total_seconds() \
                >= settings.bmtc_stop_times_every_hours * 3600:
            self.pull_stop_times()

    def _retention(self) -> None:
        log.info("retention | %s", apply_retention(datetime.now(timezone.utc)))

    def _stop(self, *_: Any) -> None:
        self.stopping = True


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    TripCollector().run()


if __name__ == "__main__":
    main()
