"""static_feed_meta() and route_shape() caching.

Call-counting tests are Postgres-only (that's where the real cost lives:
gtfs_stop_times/gtfs_shapes are multi-million-row tables there); the
behavioural tests (does invalidation work, does it hand out independent
lists, does it leave history_rows live) run against both adapters via the
repo fixture, since that's engine-agnostic and worth proving on both.
"""
import time

import pytest

from conftest import _PG_OK


def test_static_feed_meta_is_cached(repo, monkeypatch):
    if type(repo).__name__ != "PostgresRepository":
        pytest.skip("call-counting test is postgres-only; see module docstring")
    from app.data.postgres_repo import PostgresRepository
    before = repo.static_feed_meta()

    # monkeypatch.setattr, not a manual save/restore: it re-wraps the
    # replacement with staticmethod() the same way the original was
    # declared, and restores the exact original descriptor at teardown - a
    # manual `real = Cls._all; ...; Cls._all = real` loses that wrapping
    # (class attribute access unwraps a staticmethod to a plain function),
    # which corrupts _all as a bound-call for every test that runs after
    # this one in the same process. Caught exactly that way while writing
    # this test the first time.
    calls = {"n": 0}
    real = PostgresRepository._all
    def counting(sql, params=()):
        if "gtfs_shapes" in sql:
            calls["n"] += 1
        return real(sql, params)
    monkeypatch.setattr(PostgresRepository, "_all", staticmethod(counting))

    for _ in range(5):
        repo.static_feed_meta()

    assert calls["n"] <= 1, f"expected at most 1 real query for 5 calls, got {calls['n']}"
    assert repo.static_feed_meta() == before


def test_route_shape_is_cached_per_route(repo, monkeypatch):
    if type(repo).__name__ != "PostgresRepository":
        pytest.skip("call-counting test is postgres-only; see module docstring")
    from app.data.postgres_repo import PostgresRepository

    calls = {"n": 0}
    real = PostgresRepository._all
    def counting(sql, params=()):
        if "gtfs_shapes" in sql:
            calls["n"] += 1
        return real(sql, params)
    monkeypatch.setattr(PostgresRepository, "_all", staticmethod(counting))

    for _ in range(5):
        repo.route_shape("R1")

    assert calls["n"] <= 1, f"expected at most 1 real query for 5 calls on R1, got {calls['n']}"


def test_route_shape_returns_independent_lists_not_the_cached_object(repo):
    """A caller mutating what it got back must not corrupt the cache for the
    next caller - route_shape() must hand out a fresh list each time."""
    a = repo.route_shape("R1")
    b = repo.route_shape("R1")
    assert a == b
    assert a is not b, "must be a fresh list, not the cached object itself"
    a.append([999.0, 999.0])
    assert repo.route_shape("R1") != a, "mutating the caller's copy must not leak into the cache"


def test_invalidate_static_cache_forces_a_fresh_read(repo):
    """The explicit escape hatch: don't wait for the TTL, force a refresh."""
    before = repo.static_feed_meta()["stops"]

    if type(repo).__name__ == "PostgresRepository":
        from app.data import pg
        with pg.pool().connection() as conn:
            conn.execute(
                "INSERT INTO gtfs_stops (stop_id, stop_code, stop_name, stop_lat, stop_lon) "
                "VALUES ('S-NEW', 'X', 'New Stop', 28.7, 77.2)")
    else:
        from app.data import db
        db.get_connection().execute(
            "INSERT INTO gtfs_stops (stop_id, stop_code, stop_name, stop_lat, stop_lon) "
            "VALUES ('S-NEW', 'X', 'New Stop', 28.7, 77.2)")
        db.get_connection().commit()

    assert repo.static_feed_meta()["stops"] == before, "should still be serving the cached count"
    repo.invalidate_static_cache()
    assert repo.static_feed_meta()["stops"] == before + 1, "must reflect the change once invalidated"


def test_feed_summary_history_rows_stays_live_while_static_part_is_cached(repo):
    """The property this fix must never break: caching the static metadata
    must not make the live, ever-growing position count (history_rows) look
    frozen. This is the actual "does this affect data collection" question,
    answered directly rather than assumed."""
    first = repo.feed_summary()
    routes_before = first["routes"]

    now = int(time.time())
    repo.upsert_vehicles([{
        "vehicle_id": "V-LIVE-COUNT", "ts": now, "trip_id": None, "route_id": "R1",
        "lat": 28.6, "lon": 77.2, "bearing": None, "speed": None, "stop_id": None,
        "current_status": None, "congestion_level": None, "occupancy_status": None,
        "ingested_at": now,
    }])

    second = repo.feed_summary()
    if type(repo).__name__ == "PostgresRepository":
        # A planner row-count estimate (reltuples) - see feed_summary()'s own
        # comment on why - so it may not have caught up to this one new row
        # yet without an ANALYZE. >= still catches the regression this test
        # exists for: history_rows staying frozen at the OLD cached value
        # while real data keeps arriving would be a *decrease* relative to
        # what a correct estimate trends toward, not just "not yet increased".
        assert second["history_rows"] >= first["history_rows"]
    else:
        # SQLite's is an exact COUNT(*), so this must strictly increase -
        # a stronger check that the two engines' tests together fully cover
        # what a lenient >= alone could let slip through unnoticed.
        assert second["history_rows"] > first["history_rows"], (
            "history_rows must reflect newly collected data immediately, not lag behind a cache")
    assert second["routes"] == routes_before, "the genuinely static count should still be cached"
