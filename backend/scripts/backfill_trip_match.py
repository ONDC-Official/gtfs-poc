#!/usr/bin/env python3
"""One-time backfill for live_trip_match.

    python -m scripts.backfill_trip_match

Existing history predates this table, so Aggregator._match_new_trips would
otherwise have to catch up TRIP_MATCH_CHUNK_S (1 day) per aggregator tick
(currently every 6h) - workable, but slow for a deployment with days of
retained history. This script runs the identical match, day by day, back to
back, so a large backlog catches up in one run instead of over many days.

Safe to run against a live deployment: each chunk's upsert_trip_matches call
is independent of the others, and it picks up from wherever the watermark
already is - re-running after a partial run, or after the live aggregator has
advanced it further in the meantime, just does less work.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings                                     # noqa: E402
from app.data import db                                             # noqa: E402
from app.services.aggregator import (TRIP_MATCH_WATERMARK_KEY,       # noqa: E402
                                     TRIP_MATCH_CHUNK_S)
from app.services.trip_matcher import TripMatcher                   # noqa: E402


def _load_repository():
    if settings.db_backend == "postgres":
        from app.data.postgres_repo import repository as repo
    else:
        from app.data.sqlite_repo import repository as repo
    return repo


def main():
    db.init_db()
    repo = _load_repository()
    matcher = TripMatcher(repo)
    matcher.refresh()

    newest = repo.newest_observation_ts()
    if not newest:
        print("no observations yet - nothing to backfill")
        return

    mark = repo.meta_get(TRIP_MATCH_WATERMARK_KEY)
    since = int(mark) if mark else max(0, newest - settings.history_retention_hours * 3600)
    if since >= newest:
        print("trip match is already caught up (watermark={0})".format(since))
        return

    print("backfilling trip match: {0} -> {1} ({2:.1f}h) in {3}s chunks".format(
        since, newest, (newest - since) / 3600, TRIP_MATCH_CHUNK_S))

    chunks = matched_total = 0
    started = time.time()
    while since < newest:
        until = min(since + TRIP_MATCH_CHUNK_S, newest)
        t0 = time.time()
        live_trips = repo.distinct_live_trip_keys(since, until)
        now = int(time.time())
        rows = []
        for lt in live_trips:
            result = matcher.match(lt["route_id"], lt["trip_id"])
            rows.append({
                "live_trip_id": lt["trip_id"], "vehicle_id": lt["vehicle_id"],
                "route_id": lt["route_id"], "matched_trip_id": result.matched_trip_id,
                "match_type": result.match_type, "delta_minutes": result.delta_minutes,
                "matched_at": now,
            })
        repo.upsert_trip_matches(rows)
        repo.meta_set(TRIP_MATCH_WATERMARK_KEY, str(int(until)))
        chunks += 1
        matched_total += len(rows)
        print("  [{0}] {1} -> {2}: {3} trips ({4:.1f}s)".format(
            chunks, since, until, len(rows), time.time() - t0))
        since = until

    print("done: {0} chunks, {1} trips matched, {2:.1f}s, watermark now {3}".format(
        chunks, matched_total, time.time() - started, newest))


if __name__ == "__main__":
    main()
