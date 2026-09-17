#!/usr/bin/env python3
"""One-time backfill for the continuity rollup (analytics_continuity_hour,
analytics_vehicle_gap_state, analytics_trip_gap_state).

    python -m scripts.backfill_continuity

Existing history predates these tables, so `Aggregator._fold_continuity`
would otherwise have to catch up one CONTINUITY_CHUNK_S (1 day) chunk per
aggregator tick (120s) - workable, but slow for a deployment with weeks of
retained history. This script runs the identical fold, day by day, back to
back, so a large backlog catches up in one run instead of over many hours.

Safe to run against a live deployment: each chunk is its own transaction
(see Repository.fold_continuity_gaps), and it picks up from wherever the
watermark already is - re-running after a partial run, or after the live
aggregator has advanced it further in the meantime, just does less work.
Idempotent only in the sense that catching up to "now" twice in a row is a
no-op; it must NOT be pointed at an already-covered range (see the
docstring on fold_continuity_gaps for why).
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings                        # noqa: E402
from app.data import db                                # noqa: E402
from app.services.aggregator import (CONTINUITY_WATERMARK_KEY,  # noqa: E402
                                     GAP_THRESHOLD_S, CONTINUITY_CHUNK_S)


def _load_repository():
    if settings.db_backend == "postgres":
        from app.data.postgres_repo import repository as repo
    else:
        from app.data.sqlite_repo import repository as repo
    return repo


def main():
    db.init_db()
    repo = _load_repository()

    newest = repo.newest_observation_ts()
    if not newest:
        print("no observations yet - nothing to backfill")
        return

    mark = repo.meta_get(CONTINUITY_WATERMARK_KEY)
    if mark:
        since = int(mark)
    else:
        # Same clamp as Aggregator._fold_continuity: rt_vehicle_position never
        # holds anything older than history_retention_hours, so there is
        # nothing to backfill before that boundary.
        since = max(0, newest - settings.history_retention_hours * 3600)
    if since >= newest:
        print("continuity rollup is already caught up (watermark={0})".format(since))
        return

    print("backfilling continuity rollup: {0} -> {1} ({2:.1f}h) in {3}s chunks".format(
        since, newest, (newest - since) / 3600, CONTINUITY_CHUNK_S))

    chunks = 0
    started = time.time()
    while since < newest:
        until = min(since + CONTINUITY_CHUNK_S, newest)
        t0 = time.time()
        repo.fold_continuity_gaps(since, until, GAP_THRESHOLD_S)
        chunks += 1
        print("  [{0}] {1} -> {2} ({3:.1f}s)".format(chunks, since, until, time.time() - t0))
        since = until

    print("done: {0} chunks in {1:.1f}s, watermark now {2}".format(
        chunks, time.time() - started, newest))


if __name__ == "__main__":
    main()
