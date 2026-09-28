"""What happens if a static feed refresh (deleting old gtfs_routes/gtfs_trips
rows) is ever attempted, given the two foreign keys this deployment added.

Postgres-only: SQLite doesn't declare either FK (see schema.sql's comment on
live_trip_match), so there's nothing to test on that side.

This encodes a deliberate asymmetry, not an oversight - see the comments on
fk_vp_route and live_trip_match.matched_trip_id in schema_postgres.sql:

  - rt_vehicle_position.route_id is a MEASURED FACT (a bus really did report
    this route). Deleting a still-referenced gtfs_routes row must keep
    failing loudly - that's fk_vp_route's default NO ACTION.
  - live_trip_match.matched_trip_id is THIS SERVICE'S OWN best-effort
    annotation. Deleting a still-referenced gtfs_trips row must succeed and
    just clear the pointer - that's why it's ON DELETE SET NULL.
"""
import time

import pytest

from conftest import _PG_OK

pytestmark = pytest.mark.skipif(not _PG_OK, reason="no local Postgres for this test")


def _pg_only(repo):
    if type(repo).__name__ != "PostgresRepository":
        pytest.skip("postgres-only")


def test_deleting_a_referenced_route_is_still_blocked(repo):
    """The asymmetry's other half: this must NOT have been loosened by the
    live_trip_match work. A route_id real live data has reported must remain
    impossible to delete out from under that history."""
    _pg_only(repo)
    from app.data import pg
    now = int(time.time())
    repo.upsert_vehicles([{
        "vehicle_id": "V-HISTORY", "ts": now, "trip_id": None, "route_id": "R1",
        "lat": 28.6, "lon": 77.2, "bearing": None, "speed": None, "stop_id": None,
        "current_status": None, "congestion_level": None, "occupancy_status": None,
        "ingested_at": now,
    }])
    with pg.pool().connection() as conn:
        with pytest.raises(Exception, match="(?i)foreign key"):
            conn.execute("DELETE FROM gtfs_routes WHERE route_id = 'R1'")


def test_deleting_a_matched_trip_clears_the_pointer_instead_of_blocking(repo):
    """A refresh removing a scheduled trip that live data was matched against
    must succeed - and the stale match becomes visibly NULL rather than
    silently wrong or blocking the whole refresh."""
    _pg_only(repo)
    from app.data import pg
    now = int(time.time())

    with pg.pool().connection() as conn:
        conn.execute(
            "INSERT INTO gtfs_trips (trip_id, route_id, service_id) VALUES (%s,%s,%s)",
            ("R1_08_00", "R1", "WD"))

    repo.upsert_vehicles([{
        "vehicle_id": "V-MATCHED", "ts": now, "trip_id": "R1_08_00_1", "route_id": "R1",
        "lat": 28.6, "lon": 77.2, "bearing": None, "speed": None, "stop_id": None,
        "current_status": None, "congestion_level": None, "occupancy_status": None,
        "ingested_at": now,
    }])

    from app.services.aggregator import Aggregator
    Aggregator(repo)._match_new_trips(now + 1)

    before = repo.trip_match("R1_08_00_1")
    assert before["matched_trip_id"] == "R1_08_00"
    assert before["match_type"] == "strict"

    # The refresh: remove the trip the match points at. Must not raise.
    with pg.pool().connection() as conn:
        conn.execute("DELETE FROM gtfs_trips WHERE trip_id = 'R1_08_00'")

    after = repo.trip_match("R1_08_00_1")
    assert after["matched_trip_id"] is None, "the dangling pointer must clear, not linger"
    # match_type is left alone - a readable historical record, not silently
    # rewritten - per the schema comment.
    assert after["match_type"] == "strict"
