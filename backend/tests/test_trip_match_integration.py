"""TripMatcher wired into the Aggregator and the quality tier, against the
real repo fixture (both adapters) - this is where a mismatch between
TripMatcher's expectations and what the repo actually returns would show up,
which test_trip_matcher.py's fake-repo tests structurally cannot catch.

The seeded DATASET's own trip_ids (T1, T2, ...) don't have the route_HH_MM
shape a real trip_id has, so a couple of dispatch-shaped rows are inserted
directly here rather than relying on conftest's fixture data.
"""
import time

from conftest import NOW
from app.services.aggregator import Aggregator, TRIP_MATCH_WATERMARK_KEY


def _insert_static_trip(repo, trip_id: str, route_id: str) -> None:
    """conftest's DATASET has no route_HH_MM-shaped trip_id to match against
    - insert one directly, engine-appropriate, rather than teach the shared
    fixture about a format only this test needs."""
    if type(repo).__name__ == "PostgresRepository":
        from app.data import pg
        with pg.pool().connection() as conn:
            conn.execute(
                "INSERT INTO gtfs_trips (trip_id, route_id, service_id) VALUES (%s,%s,%s)",
                (trip_id, route_id, "WD"))
    else:
        from app.data import db
        c = db.get_connection()
        c.execute(
            "INSERT INTO gtfs_trips (trip_id, route_id, service_id) VALUES (?,?,?)",
            (trip_id, route_id, "WD"))
        c.commit()


def test_match_new_trips_populates_live_trip_match(repo):
    _insert_static_trip(repo, "R1_08_00", "R1")   # matches T1's own route, R1

    since = int(NOW) - 3600
    now = int(NOW)
    repo.upsert_vehicles([{
        "vehicle_id": "V-DISPATCH", "ts": now, "trip_id": "R1_08_00_1",
        "route_id": "R1", "lat": 28.6, "lon": 77.2, "bearing": None, "speed": None,
        "stop_id": None, "current_status": None, "congestion_level": None,
        "occupancy_status": None, "ingested_at": now,
    }])

    agg = Aggregator(repo)
    result = agg._match_new_trips(now + 1)

    assert result["matched"] >= 1
    match = repo.trip_match("R1_08_00_1")
    assert match is not None
    assert match["match_type"] == "strict"
    assert match["matched_trip_id"] == "R1_08_00"
    assert match["route_id"] == "R1"
    assert match["vehicle_id"] == "V-DISPATCH"

    # Watermark must have advanced to (at most) the point we asked it to.
    mark = repo.meta_get(TRIP_MATCH_WATERMARK_KEY)
    assert mark is not None
    assert int(mark) <= now + 1


def test_match_new_trips_relaxed_and_none(repo):
    _insert_static_trip(repo, "R1_09_00", "R1")

    now = int(NOW)
    repo.upsert_vehicles([
        {"vehicle_id": "V-RELAXED", "ts": now, "trip_id": "R1_09_10_1", "route_id": "R1",
         "lat": 28.6, "lon": 77.2, "bearing": None, "speed": None, "stop_id": None,
         "current_status": None, "congestion_level": None, "occupancy_status": None,
         "ingested_at": now},
        {"vehicle_id": "V-NOMATCH", "ts": now, "trip_id": "R1_23_00_1", "route_id": "R1",
         "lat": 28.6, "lon": 77.2, "bearing": None, "speed": None, "stop_id": None,
         "current_status": None, "congestion_level": None, "occupancy_status": None,
         "ingested_at": now},
    ])

    agg = Aggregator(repo)
    agg._match_new_trips(now + 1)

    relaxed = repo.trip_match("R1_09_10_1")
    assert relaxed["match_type"] == "relaxed"
    assert relaxed["matched_trip_id"] == "R1_09_00"
    assert relaxed["delta_minutes"] == 10.0

    none_match = repo.trip_match("R1_23_00_1")
    assert none_match["match_type"] == "none"
    assert none_match["matched_trip_id"] is None


def test_trip_match_summary_counts_by_type(repo):
    _insert_static_trip(repo, "R1_10_00", "R1")
    now = int(time.time())
    before = repo.trip_match_summary(0)["total"]

    repo.upsert_vehicles([
        {"vehicle_id": "V-A", "ts": now, "trip_id": "R1_10_00_1", "route_id": "R1",
         "lat": 28.6, "lon": 77.2, "bearing": None, "speed": None, "stop_id": None,
         "current_status": None, "congestion_level": None, "occupancy_status": None,
         "ingested_at": now},
        {"vehicle_id": "V-B", "ts": now, "trip_id": "R1_23_59_1", "route_id": "R1",
         "lat": 28.6, "lon": 77.2, "bearing": None, "speed": None, "stop_id": None,
         "current_status": None, "congestion_level": None, "occupancy_status": None,
         "ingested_at": now},
    ])
    Aggregator(repo)._match_new_trips(now + 1)

    summary = repo.trip_match_summary(0)
    assert summary["total"] == before + 2
    assert summary["strict"] >= 1
    assert summary["none"] >= 1
    assert 0 <= summary["linked_pct"] <= 100


def test_quality_correctness_exposes_schedule_link(repo):
    """Wiring check: QualityService.correctness() must expose schedule_link
    from trip_match_summary without crashing, whatever the current data looks
    like (including the all-none case from unmatched fixture trip_ids)."""
    from app.services.quality import QualityService
    q = QualityService(repo)
    result = q.correctness(hours=24)
    assert "schedule_link" in result
    link = result["schedule_link"]
    assert set(link) == {"total", "strict", "relaxed", "none", "strict_pct", "linked_pct"}
