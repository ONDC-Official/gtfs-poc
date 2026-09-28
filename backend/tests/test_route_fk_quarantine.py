"""Postgres-only: the route_id foreign key on rt_vehicle_position, and the
quarantine path that keeps a batch with one bad route from losing every
other vehicle in that poll.

Skipped everywhere the Postgres adapter isn't reachable (see conftest.py's
GTFS_TEST_PG_DSN / _PG_OK) - there's nothing to test against on SQLite,
which doesn't get this constraint.
"""
import time

import pytest

from conftest import _PG_OK

pytestmark = pytest.mark.skipif(not _PG_OK, reason="no local Postgres for this test")


def _row(vehicle_id, route_id, ts):
    return {"vehicle_id": vehicle_id, "ts": ts, "trip_id": None, "route_id": route_id,
            "lat": 28.6, "lon": 77.2, "bearing": None, "speed": None, "stop_id": None,
            "current_status": None, "congestion_level": None, "occupancy_status": None,
            "ingested_at": ts}


def test_fk_constraint_exists_on_every_partition(repo):
    """Postgres 16 won't take a NOT VALID FK on the partitioned parent
    itself (confirmed against a real instance), so this is declared per
    partition instead - see schema_postgres.sql's comment on fk_vp_route.
    Every partition that exists, including the default one, must have it."""
    if type(repo).__name__ != "PostgresRepository":
        pytest.skip("postgres-only")
    from app.data import pg
    with pg.pool().connection() as conn:
        partitions = [r["part"] for r in conn.execute(
            "SELECT i.inhrelid::regclass::text AS part FROM pg_inherits i "
            "WHERE i.inhparent = 'rt_vehicle_position'::regclass").fetchall()]
        assert partitions, "expected at least the default partition to exist"
        missing = [p for p in partitions if not conn.execute(
            "SELECT 1 FROM pg_constraint WHERE conname = 'fk_vp_route' "
            "AND conrelid = %s::regclass", (p,)).fetchone()]
    assert not missing, f"partitions missing fk_vp_route: {missing}"


def test_new_partition_gets_the_fk_automatically(repo):
    """rt_ensure_day_partition() must add fk_vp_route to every partition it
    creates - the whole point of putting it there instead of only backfilling
    the ones that exist today."""
    if type(repo).__name__ != "PostgresRepository":
        pytest.skip("postgres-only")
    from app.data import pg
    far_future_ts = int(time.time()) + 400 * 86400
    with pg.pool().connection() as conn:
        conn.execute("SELECT rt_ensure_day_partition(%s)", (far_future_ts,))
        part = conn.execute(
            "SELECT i.inhrelid::regclass::text AS part FROM pg_inherits i "
            "JOIN pg_class c ON c.oid = i.inhrelid "
            "WHERE i.inhparent = 'rt_vehicle_position'::regclass "
            "AND c.relname LIKE 'rt_vehicle_position_2%' "
            "ORDER BY c.relname DESC LIMIT 1").fetchone()["part"]
        row = conn.execute(
            "SELECT convalidated FROM pg_constraint WHERE conname = 'fk_vp_route' "
            "AND conrelid = %s::regclass", (part,)).fetchone()
    assert row is not None, f"new partition {part} should get fk_vp_route immediately"
    assert row["convalidated"] is True, "a brand-new empty partition's FK should validate instantly"


def test_known_route_is_stored_normally(repo):
    if type(repo).__name__ != "PostgresRepository":
        pytest.skip("postgres-only")
    now = int(time.time())
    before = repo.deadletter_count()
    # DATASET's seeded routes include route_id "R1" (see conftest.py's fixture data).
    inserted = repo.upsert_vehicles([_row("V-KNOWN", "R1", now)])
    assert inserted == 1
    assert repo.deadletter_count() == before, "a known route must never be quarantined"


def test_unknown_route_is_quarantined_not_dropped_and_not_stored(repo):
    if type(repo).__name__ != "PostgresRepository":
        pytest.skip("postgres-only")
    now = int(time.time())
    bogus = "NOT_A_REAL_ROUTE_9999"
    before = repo.deadletter_count()
    inserted = repo.upsert_vehicles([_row("V-BOGUS", bogus, now)])
    assert inserted == 0, "a row with an unknown route must not land in rt_vehicle_position"
    assert repo.deadletter_count() == before + 1, "but it must not just vanish either"
    latest = repo.latest_vehicles(None, None, None)
    assert not any(v["vehicle_id"] == "V-BOGUS" for v in latest)


def test_one_bad_route_does_not_sink_the_rest_of_the_batch(repo):
    """The actual production bug this guards against: without filtering
    first, one bad row inside upsert_vehicles' single executemany() would
    roll back the whole poll - every other vehicle in it too."""
    if type(repo).__name__ != "PostgresRepository":
        pytest.skip("postgres-only")
    now = int(time.time())
    before = repo.deadletter_count()
    batch = [_row(f"V-GOOD-{i}", "R1", now) for i in range(5)]
    batch.insert(2, _row("V-BOGUS-2", "STILL_NOT_REAL", now))
    inserted = repo.upsert_vehicles(batch)
    assert inserted == 5, "all 5 legitimate vehicles in the batch must still be stored"
    assert repo.deadletter_count() == before + 1, (
        "exactly the one bad row in this batch should be quarantined, no more, no less")
    stored_ids = {v["vehicle_id"] for v in repo.latest_vehicles(None, None, None)}
    for i in range(5):
        assert f"V-GOOD-{i}" in stored_ids


def test_null_route_id_is_not_quarantined(repo):
    """route_id is nullable and the FK doesn't constrain NULLs - a vehicle
    with no route yet (feed hasn't assigned one) must still be stored."""
    if type(repo).__name__ != "PostgresRepository":
        pytest.skip("postgres-only")
    now = int(time.time())
    inserted = repo.upsert_vehicles([_row("V-NOROUTE", None, now)])
    assert inserted == 1
    stored_ids = {v["vehicle_id"] for v in repo.latest_vehicles(None, None, None)}
    assert "V-NOROUTE" in stored_ids


def test_known_route_set_is_cached_not_queried_every_poll(repo, monkeypatch):
    """The actual cost issue this guards against: before caching,
    upsert_vehicles queried gtfs_routes once per call - once per poll, every
    RT_POLL_SECONDS - just to filter routes that essentially never appear.
    Several calls in a row must hit the database at most once."""
    if type(repo).__name__ != "PostgresRepository":
        pytest.skip("postgres-only")
    from app.data.postgres_repo import PostgresRepository
    calls = {"n": 0}
    real_all = PostgresRepository._all

    def counting_all(sql, params=()):
        if "gtfs_routes" in sql:
            calls["n"] += 1
        return real_all(sql, params)
    monkeypatch.setattr(PostgresRepository, "_all", staticmethod(counting_all))

    now = int(time.time())
    for i in range(10):
        repo.upsert_vehicles([_row(f"V-POLL-{i}", "R1", now)])
    assert calls["n"] <= 1, (
        f"expected gtfs_routes to be queried at most once for 10 polls, got {calls['n']}")


def test_known_route_cache_refreshes_after_ttl(repo):
    """The cache must not be permanent - a genuinely new route (the static
    feed got reloaded) has to become visible eventually, not require a
    restart."""
    if type(repo).__name__ != "PostgresRepository":
        pytest.skip("postgres-only")
    from app.data.postgres_repo import KNOWN_ROUTES_TTL_S
    now = int(time.time())

    assert "R1" in repo._known_route_ids()   # populates the cache

    # A brand-new route added after the cache was populated must not be
    # visible until the TTL passes - proves this is really cached, not
    # re-fetched every call (which the previous test already checks too).
    from app.data import pg
    with pg.pool().connection() as conn:
        conn.execute("INSERT INTO gtfs_routes (route_id, agency_id, route_short_name, "
                     "route_long_name, route_desc, route_type) VALUES "
                     "('R-NEW','DTC','new','new','',3)")
    assert "R-NEW" not in repo._known_route_ids(), "should still be serving the cached set"

    # Force the cache to look expired without actually sleeping in a test.
    repo._route_cache._at["gtfs_routes.route_id"] -= KNOWN_ROUTES_TTL_S + 1
    assert "R-NEW" in repo._known_route_ids(), "must refresh once the TTL has passed"
