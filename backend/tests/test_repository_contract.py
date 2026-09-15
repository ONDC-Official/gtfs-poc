"""The Repository contract.

Every method of the port, asserted against behaviour rather than against a
particular engine. An adapter that passes this suite is substitutable.

Several tests are marked DIVERGENCE: they encode behaviour where SQLite and
Postgres differ silently - the same query returns different answers rather
than raising. Those are the ones that make this suite worth having.
"""
import math

import pytest

from conftest import (CELL_X, CELL_Y, COSLAT, HOUR, HUB_LAT, HUB_LON, M_PER_DEG,
                      NOW, PROBE_LAT, PROBE_LON)
from app.services.aggregator import (GRID_LAT_DEG, GRID_LON_DEG, HOUR,
                                     IMPLAUSIBLE_MPS, MOVING_MPS)


# ---------------------------------------------------------------------------
# Static schedule
# ---------------------------------------------------------------------------

def test_feed_summary_counts_every_table(repo):
    s = repo.feed_summary()
    assert s["agencies"] == 2
    assert s["routes"] == 4
    assert s["stops"] == 5
    assert s["trips"] == 4
    assert s["stop_times"] == 7
    assert s["shape_points"] == 8
    assert s["history_rows"] == 8
    assert s["static_loaded_at"] == NOW - 86400
    # S5 "Probe Corner" sits south of the hub, S4 "Far Depot" north of it.
    assert s["bbox"]["min_lat"] == pytest.approx(PROBE_LAT)
    assert s["bbox"]["max_lat"] == pytest.approx(HUB_LAT + 0.02)


def test_list_routes_unfiltered(repo):
    page = repo.list_routes(None, 50, 0)
    assert page["total"] == 4
    assert {r["route_id"] for r in page["items"]} == {"R1", "R2", "R3", "R4"}
    # trip_count is a correlated subquery, not a join artefact
    assert {r["route_id"]: r["trip_count"] for r in page["items"]}["R1"] == 2


def test_list_routes_pagination(repo):
    first = repo.list_routes(None, 2, 0)
    second = repo.list_routes(None, 2, 2)
    assert len(first["items"]) == len(second["items"]) == 2
    assert first["total"] == second["total"] == 4
    assert not ({r["route_id"] for r in first["items"]}
                & {r["route_id"] for r in second["items"]})


def test_list_routes_search_matches_short_name(repo):
    page = repo.list_routes("764", 50, 0)
    assert [r["route_id"] for r in page["items"]] == ["R1"]
    assert page["total"] == 1


def test_list_routes_search_is_case_insensitive(repo):
    """DIVERGENCE: seeded desc is 'Via RING Road'.

    SQLite's LIKE folds ASCII case; Postgres' LIKE does not. A Postgres
    adapter must use ILIKE or this silently returns nothing - no error, just
    a search box that stops working.
    """
    assert [r["route_id"] for r in repo.list_routes("ring", 50, 0)["items"]] == ["R1"]
    assert [r["route_id"] for r in repo.list_routes("RING", 50, 0)["items"]] == ["R1"]


def test_get_route(repo):
    assert repo.get_route("R1")["route_short_name"] == "764"
    assert repo.get_route("NOPE") is None


def test_route_shape_picks_the_longest_variant(repo):
    """R1 has SH1 (5 pts) and SH2 (3 pts); the fuller corridor wins."""
    coords = repo.route_shape("R1")
    assert len(coords) == 5
    assert coords[0] == [pytest.approx(HUB_LON), pytest.approx(HUB_LAT)]
    # [lon, lat] order, GeoJSON convention
    assert coords[1][0] > coords[0][0]
    assert repo.route_shape("R4") == []


def test_route_stops_uses_the_representative_trip(repo):
    """T1 (3 stops) over T2 (2 stops), ordered by stop_sequence."""
    stops = repo.route_stops("R1")
    assert [s["stop_id"] for s in stops] == ["S1", "S2", "S3"]
    assert [s["stop_sequence"] for s in stops] == [1, 2, 3]
    assert stops[0]["stop_name"] == "Connaught Place"
    assert repo.route_stops("R3") == []


def test_list_stops_filters(repo):
    assert len(repo.list_stops(None, None, 100)) == 5
    assert [s["stop_id"] for s in repo.list_stops("Janpath", None, 100)] == ["S3"]
    box = (HUB_LON - 0.0005, HUB_LAT - 0.0005, HUB_LON + 0.0005, HUB_LAT + 0.0005)
    assert [s["stop_id"] for s in repo.list_stops(None, box, 100)] == ["S1"]
    assert len(repo.list_stops(None, None, 2)) == 2


def test_list_stops_search_is_case_insensitive(repo):
    """DIVERGENCE: same LIKE/ILIKE trap as routes."""
    assert [s["stop_id"] for s in repo.list_stops("janpath", None, 100)] == ["S3"]


def test_stop_schedule_window_and_order(repo):
    rows = repo.stop_schedule("S1", 0, 200000, 50)
    assert [r["departure_s"] for r in rows] == [28830, 32430]
    assert rows[0]["route_short_name"] == "764"
    # window is inclusive on both ends
    assert len(repo.stop_schedule("S1", 28830, 28830, 50)) == 1
    assert repo.stop_schedule("S1", 0, 1000, 50) == []


def test_stop_schedule_keeps_times_past_midnight(repo):
    """GTFS allows >24:00:00; 25:00:30 must survive as 90030 seconds."""
    rows = repo.stop_schedule("S4", 0, 200000, 50)
    assert [r["departure_s"] for r in rows] == [90030]
    assert rows[0]["departure_time"] == "25:00:30"


def test_route_names_bulk_lookup(repo):
    names = repo.route_names(["R1", "R2", "NOPE", None])
    assert set(names) == {"R1", "R2"}
    assert names["R1"]["route_short_name"] == "764"
    assert names["R1"]["agency_id"] == "DTC"
    assert repo.route_names([]) == {}


def test_route_names_chunks_beyond_the_param_limit(repo):
    """SQLITE_MAX_VARIABLE_NUMBER is 999; the adapter chunks at 500."""
    ids = ["R1"] + ["X{0}".format(i) for i in range(1200)]
    assert set(repo.route_names(ids)) == {"R1"}


def test_route_count(repo):
    assert repo.route_count() == 4


# ---------------------------------------------------------------------------
# Nearby routes (PostGIS ST_DWithin replaces the hand-rolled maths in Phase 3)
# ---------------------------------------------------------------------------

def test_routes_nearby_geometry_and_grouping(repo):
    """S1 at 0 m, S2 at 111.3 m, S3 at 97.7 m, S4 at 2226 m.

    Trip T1 runs R1 over S1-S2-S3, so R1 is in range via all three; R2 (T3)
    only via S3. R4 is served by S4 alone, which is outside 500 m.
    """
    got = repo.routes_nearby(HUB_LAT, HUB_LON, 500, 50)
    assert [e["route_id"] for e in got] == ["R1", "R2"]
    assert got[0]["distance_m"] == 0.0
    assert sorted(got[0]["stops"]) == ["Barakhamba Road", "Connaught Place", "Janpath"]
    assert got[1]["stops"] == ["Janpath"]
    # longitude scaled by cos(lat), not treated as equal to latitude
    assert got[1]["distance_m"] == pytest.approx(0.001 * M_PER_DEG * COSLAT, abs=0.5)
    assert got[0]["short_name"] == "764" and got[0]["agency_id"] == "DTC"


def test_routes_nearby_radius_is_a_circle_not_a_box(repo):
    """A stop at the bbox corner is outside the circle and must be excluded."""
    corner = 400 / M_PER_DEG
    got = repo.routes_nearby(HUB_LAT - corner, HUB_LON - corner / COSLAT, 500, 50)
    diagonal = math.hypot(400, 400)
    assert diagonal > 500
    assert "R1" not in [e["route_id"] for e in got]


def test_routes_nearby_sorted_and_limited(repo):
    got = repo.routes_nearby(HUB_LAT, HUB_LON, 3000, 50)
    assert [e["distance_m"] for e in got] == sorted(e["distance_m"] for e in got)
    assert len(repo.routes_nearby(HUB_LAT, HUB_LON, 3000, 1)) == 1


def test_routes_nearby_empty_is_not_an_error(repo):
    assert repo.routes_nearby(0.0, 0.0, 500, 50) == []


def test_nearest_stop(repo):
    assert repo.nearest_stop(HUB_LAT, HUB_LON, 0.01)["stop_id"] == "S1"
    assert repo.nearest_stop(0.0, 0.0, 0.01) is None


# ---------------------------------------------------------------------------
# Realtime writes
# ---------------------------------------------------------------------------

def _obs(vid, ts, **kw):
    row = {"vehicle_id": vid, "ts": ts, "trip_id": None, "route_id": "R1",
           "lat": HUB_LAT, "lon": HUB_LON, "bearing": 0.0, "speed": 4.0,
           "ingested_at": ts}
    row.update(kw)
    return row


def test_upsert_returns_newly_inserted_count(repo):
    """DIVERGENCE: SQLite counts via conn.total_changes, which has no Postgres
    equivalent. Get this wrong and rt_poll_log.new_rows is quietly false."""
    assert repo.upsert_vehicles([_obs("NEW1", NOW + 10), _obs("NEW2", NOW + 10)]) == 2
    assert repo.upsert_vehicles([]) == 0


def test_upsert_is_idempotent_on_the_composite_key(repo):
    """Replaying the same (vehicle_id, ts) must not inflate history."""
    rows = [_obs("V1", NOW - 60)]          # already seeded
    assert repo.upsert_vehicles(rows) == 0
    assert len(repo.vehicle_history("V1", 0)) == 4


def test_upsert_latest_advances_on_newer_observation(repo):
    repo.upsert_vehicles([_obs("V1", NOW + 500, speed=15.0)])
    latest = {r["vehicle_id"]: r for r in repo.latest_vehicles(None, None, None)}
    assert latest["V1"]["ts"] == NOW + 500
    assert latest["V1"]["speed"] == 15.0


def test_upsert_latest_ignores_older_observation(repo):
    """The guard is WHERE excluded.ts >= existing.ts - out-of-order arrivals
    must never roll the projection backwards."""
    repo.upsert_vehicles([_obs("V1", NOW - 9999, speed=99.0)])
    latest = {r["vehicle_id"]: r for r in repo.latest_vehicles(None, None, None)}
    assert latest["V1"]["ts"] == NOW - 60
    assert latest["V1"]["speed"] == 9.0


# ---------------------------------------------------------------------------
# Realtime reads
# ---------------------------------------------------------------------------

def test_latest_vehicles_is_one_row_per_vehicle(repo):
    rows = repo.latest_vehicles(None, None, None)
    ids = [r["vehicle_id"] for r in rows]
    assert len(ids) == len(set(ids)) == 4          # V1 V2 V3 SIM-1


def test_latest_vehicles_filters(repo):
    assert {r["vehicle_id"] for r in repo.latest_vehicles("R2", None, None)} == {"V2"}
    box = (HUB_LON - 0.001, HUB_LAT - 0.001, HUB_LON + 0.001, HUB_LAT + 0.001)
    assert "V3" not in {r["vehicle_id"] for r in repo.latest_vehicles(None, box, None)}


def test_vehicle_history_ordered_and_bounded(repo):
    rows = repo.vehicle_history("V1", 0)
    assert [r["ts"] for r in rows] == sorted(r["ts"] for r in rows)
    assert len(rows) == 4
    assert repo.vehicle_history("NOPE", 0) == []


def test_vehicle_history_window_is_closed_on_both_ends(repo):
    """Powers the from/to API; both bounds inclusive."""
    rows = repo.vehicle_history("V1", NOW - 2 * HOUR, NOW - 1 * HOUR)
    assert [r["ts"] for r in rows] == [NOW - 2 * HOUR, NOW - HOUR]
    exact = repo.vehicle_history("V1", NOW - HOUR, NOW - HOUR)
    assert [r["ts"] for r in exact] == [NOW - HOUR]


def test_vehicle_history_until_none_means_unbounded(repo):
    assert len(repo.vehicle_history("V1", 0, None)) == 4


def test_log_poll_and_recent_polls(repo):
    repo.log_poll(polled_at=NOW, ok=True, source="feed", http_status=200,
                  entity_count=10, new_rows=5, feed_timestamp=NOW - 5,
                  latency_ms=100, error=None)
    polls = repo.recent_polls(10)
    assert polls[0]["polled_at"] == NOW           # newest first
    assert polls[0]["ok"] == 1
    assert len(repo.recent_polls(2)) == 2


def test_prune_history_respects_the_cutoff(repo):
    removed = repo.prune_history(NOW - 2 * HOUR)
    assert removed == 2                            # V1@-3h and V3@-40000
    assert {r["ts"] for r in repo.vehicle_history("V1", 0)} == {
        NOW - 2 * HOUR, NOW - HOUR, NOW - 60}
    # the projection is deliberately left intact
    assert len(repo.latest_vehicles(None, None, None)) == 4


def test_purge_simulated_removes_only_sim_rows(repo):
    assert repo.purge_simulated() == 1
    assert "SIM-1" not in {r["vehicle_id"] for r in repo.latest_vehicles(None, None, None)}
    assert repo.vehicle_history("SIM-1", 0) == []
    assert len(repo.vehicle_history("V1", 0)) == 4


def test_dark_routes_are_scheduled_but_silent(repo):
    """R4 is the only route with no vehicle reporting."""
    dark = repo.dark_routes(10)
    assert [r["route_id"] for r in dark] == ["R4"]
    assert dark[0]["trip_count"] == 1


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

def test_fleet_stats(repo):
    stats = repo.fleet_stats(10 ** 9)               # window wide enough for all
    assert stats["active_vehicles"] == 4
    assert stats["active_routes"] == 3              # R1 R2 R3
    assert stats["moving"] == 2                     # V1 9.0 and SIM-1 6.0


def test_active_routes_ranked_by_vehicle_count(repo):
    rows = repo.active_routes(10)
    assert rows[0]["vehicles"] >= rows[-1]["vehicles"]
    assert {r["route_id"] for r in rows} == {"R1", "R2", "R3"}


def test_ingest_timeseries_buckets(repo):
    rows = repo.ingest_timeseries(HOUR, 0)
    assert rows == sorted(rows, key=lambda r: r["bucket"])
    assert sum(r["observations"] for r in rows) == 8
    assert all(r["bucket"] % HOUR == 0 for r in rows)


def test_speed_histogram_bin_boundaries(repo):
    """DIVERGENCE: bin = MIN(CAST(speed / 2 AS INTEGER), bins - 1).

    Seeded speed 5.0 gives 5.0/2 = 2.5. SQLite CAST truncates to 2; Postgres
    CAST rounds to 3. A Postgres adapter must FLOOR, or every bin boundary
    shifts and the distribution chart silently lies.
    """
    # seeded speeds: 5.0 11.0 7.0 9.0 (V1), 3.0 0.0 (V2), 6.0 (SIM-1)
    bins = {r["bin"]: r["n"] for r in repo.speed_histogram(0, 12)}
    assert bins.get(2) == 1                         # 5.0/2  = 2.5 -> 2, not 3
    assert bins.get(5) == 1                         # 11.0/2 = 5.5 -> 5, not 6
    assert bins.get(3) == 2                         # 7.0/2  = 3.5 -> 3, and 6.0/2 = 3.0
    assert bins.get(4) == 1                         # 9.0/2  = 4.5 -> 4, not 5
    assert bins.get(1) == 1                         # 3.0/2  = 1.5 -> 1, not 2
    assert bins.get(0) == 1                         # 0.0


def test_speed_histogram_clamps_into_the_top_bin(repo):
    repo.upsert_vehicles([_obs("FAST", NOW + 20, speed=500.0)])
    bins = {r["bin"]: r["n"] for r in repo.speed_histogram(0, 12)}
    assert 11 in bins and max(bins) == 11


def test_route_corridor_cell_indices_use_floor(repo):
    """DIVERGENCE: gy = CAST(lat / GRID_LAT_DEG AS INTEGER).

    PROBE_LAT is seeded at cell + 0.7, so truncation gives CELL_Y and rounding
    gives CELL_Y + 1. The aggregator uses an explicit FLOOR, so a Postgres
    adapter that leaves CAST alone puts corridors one cell off the heatmap
    they are drawn over.
    """
    cells = repo.route_corridor("R1", 0)
    assert CELL_Y == math.floor(PROBE_LAT / GRID_LAT_DEG)
    probe = [c for c in cells if c["gy"] == CELL_Y]
    assert probe, "corridor cell landed on {0}, expected {1}".format(
        sorted({c["gy"] for c in cells}), CELL_Y)
    assert probe[0]["gx"] == CELL_X
    assert probe[0]["observations"] == 2


def test_route_corridor_aggregates_speed_and_dwell(repo):
    cells = {c["gy"]: c for c in repo.route_corridor("R1", 0)}
    hub = cells[math.floor(HUB_LAT / GRID_LAT_DEG)]
    # V1 at -1h (7.0) and -60s (9.0), plus the SIM-1 leftover (6.0), all on R1
    assert hub["observations"] == 3
    assert hub["speed_n"] == 3
    assert hub["speed_sum"] == pytest.approx(22.0)
    assert repo.route_corridor("NOPE", 0) == []


def test_grid_cells_and_extent_read_the_rollup(repo):
    """Empty until the aggregator runs - these read analytics_*, not the log."""
    assert repo.grid_cells(0, 1, None, 1, 100) == []
    assert repo.hourly_series(0) == []
    extent = repo.grid_extent(0)
    assert extent["cell_hours"] == 0


# ---------------------------------------------------------------------------
# Rollup write path (the storage primitives the aggregator drives)
# ---------------------------------------------------------------------------

FOLD = dict(lat_deg=GRID_LAT_DEG, lon_deg=GRID_LON_DEG, hour_s=HOUR,
            moving_mps=MOVING_MPS, cap_mps=IMPLAUSIBLE_MPS)


def _fold(repo, since=0):
    repo.fold_rollups(since, FOLD["lat_deg"], FOLD["lon_deg"],
                      FOLD["hour_s"], FOLD["moving_mps"], FOLD["cap_mps"])


def test_meta_roundtrip(repo):
    assert repo.meta_get("nope") is None
    repo.meta_set("k", "1")
    assert repo.meta_get("k") == "1"
    repo.meta_set("k", "2")                          # upsert, not a duplicate
    assert repo.meta_get("k") == "2"


def test_newest_observation_ts(repo):
    assert repo.newest_observation_ts() == NOW - 60


def test_count_observations_since(repo):
    assert repo.count_observations_since(0) == 8
    assert repo.count_observations_since(NOW - 100) == 2    # V1@-60, V2@-90
    assert repo.count_observations_since(NOW - 130) == 3    # ...plus SIM-1@-120
    assert repo.count_observations_since(NOW + 10 ** 6) == 0


def test_fold_rollups_grid_excludes_rows_without_position(repo):
    """V3 has a null lat/lon, so it contributes to the route rollup but not
    the spatial one."""
    _fold(repo)
    cells = repo.grid_cells(0, 1, None, 1, 100_000)
    assert sum(c["observations"] for c in cells) == 7          # 8 minus V3
    assert sum(c["moving"] for c in cells) == 6                # all but the 0.0
    assert sum(c["stopped"] for c in cells) == 1
    assert sum(c["speed_sum"] for c in cells) == pytest.approx(41.0)


def test_fold_rollups_route_rollup_keeps_positionless_rows(repo):
    _fold(repo)
    series = repo.hourly_series(0)
    assert sum(h["observations"] for h in series) == 8
    assert sum(h["moving"] for h in series) == 6
    assert sum(h["stopped"] for h in series) == 1


def test_fold_rollups_buckets_align_to_the_hour(repo):
    _fold(repo)
    assert all(h["hour_bucket"] % HOUR == 0 for h in repo.hourly_series(0))


def test_fold_rollups_is_idempotent(repo):
    """The property the whole rewind strategy rests on: re-folding an already
    folded window must replace buckets, never accumulate into them."""
    _fold(repo)
    first_cells = repo.grid_cells(0, 1, None, 1, 100_000)
    first_counts = repo.rollup_counts()

    _fold(repo)
    _fold(repo)

    assert repo.rollup_counts() == first_counts
    assert repo.grid_cells(0, 1, None, 1, 100_000) == first_cells


def test_fold_rollups_since_zero_is_a_full_rebuild(repo):
    _fold(repo)
    before = repo.rollup_counts()
    assert before["cells"] > 0

    # Drop every observation, then re-fold from scratch: nothing should survive.
    repo.prune_history(NOW + 10 ** 6)
    _fold(repo)
    assert repo.rollup_counts() == {"cells": 0, "routes": 0}


def test_fold_rollups_partial_window_leaves_older_buckets_alone(repo):
    _fold(repo)
    total = repo.rollup_counts()["cells"]

    # Re-fold only the most recent hour; older buckets must be untouched.
    _fold(repo, since=(NOW // HOUR) * HOUR)
    assert repo.rollup_counts()["cells"] == total


def test_rollup_counts_are_cell_hours_not_cells(repo):
    """`cells` is COUNT(*) over analytics_grid_hour - one row per cell per
    hour. grid_cells() collapses the hour dimension, so it returns fewer.
    The aggregator's status panel reports the former; keep them distinct.
    """
    _fold(repo)
    counts = repo.rollup_counts()
    distinct_cells = len(repo.grid_cells(0, 1, None, 1, 100_000))
    assert counts["cells"] > distinct_cells > 0


# ---------------------------------------------------------------------------
# Quality tier
# ---------------------------------------------------------------------------

def test_route_coverage_by_hour_covers_every_bucket_once(repo):
    _fold(repo)
    rows = repo.route_coverage_by_hour(0)
    # Every (hour-of-day, weekend) group's `samples` counts one of the
    # distinct hour_buckets in analytics_route_hour; the groups partition
    # those buckets, so the total must match hourly_series()'s row count.
    assert sum(r["samples"] for r in rows) == len(repo.hourly_series(0))
    for r in rows:
        assert 0 <= r["hod"] <= 23
        assert r["weekend"] in (0, 1)
        assert r["avg_routes_live"] > 0


def test_distinct_grid_cells_matches_base_resolution_grid(repo):
    _fold(repo)
    # Same rollup, two read paths: distinct_grid_cells() is the count half of
    # what grid_cells() returns in full at factor=1 with no observation floor.
    assert repo.distinct_grid_cells(0) == len(repo.grid_cells(0, 1, None, 1, 100_000))


def test_continuity_report_basic(repo):
    # In-window: V1 (4 obs, hourly-ish), V2 (2 obs), SIM-1 (1 obs). V3's one
    # observation is 40,000s old and falls outside this window.
    r = repo.continuity_report(NOW - 4 * HOUR, NOW, 90)
    assert r["vehicles"] == 3
    assert r["observations"] == 7
    # V1 contributes 3 successive gaps, V2 contributes 1; SIM-1 has only one
    # row so no gap. Every one of those 4 gaps is hour-scale, so all clear a
    # 90s threshold.
    assert r["gaps_total"] == 4
    assert r["gaps_over_threshold"] == 4
    assert r["gap_buckets"]["b_600_plus"] == 4
    assert sum(r["gap_buckets"].values()) == 4
    assert r["avg_span_s"] == pytest.approx((10740 + 7110 + 0) / 3)
    assert r["avg_observations_per_vehicle"] == pytest.approx(7 / 3)


def test_continuity_report_respects_threshold(repo):
    r = repo.continuity_report(NOW - 4 * HOUR, NOW, 999_999)
    assert r["gaps_total"] == 4          # unaffected by the threshold
    assert r["gaps_over_threshold"] == 0  # nothing clears this one


def test_trip_completeness(repo):
    since = NOW - 4 * HOUR
    # V3 and SIM-1 have no trip_id, so only (V1, T1) and (V2, T3) count.
    strict = repo.trip_completeness(since, NOW, 90)
    assert strict["trips"] == 2
    assert strict["complete_trips"] == 0

    lenient = repo.trip_completeness(since, NOW, 999_999)
    assert lenient["trips"] == 2
    assert lenient["complete_trips"] == 2


def test_referential_integrity_all_valid_in_fixture(repo):
    # Every route_id/trip_id in OBSERVATIONS either exists in the static
    # schedule or is NULL (which the check skips), so this fixture only
    # exercises the "nothing invalid" path.
    r = repo.referential_integrity(0)
    assert r["total"] == 8
    assert r["invalid_route_id"] == 0
    assert r["invalid_trip_id"] == 0


def test_field_population_counts_non_null_per_field(repo):
    fields = repo.field_population(0)
    assert fields["route_id"] == {"populated": 8, "total": 8}
    assert fields["lat"] == {"populated": 7, "total": 8}       # V3 has no position
    assert fields["speed"] == {"populated": 7, "total": 8}     # V3 has no speed
    assert fields["trip_id"] == {"populated": 6, "total": 8}   # V3, SIM-1 have none
    # Never populated by this feed / this fixture.
    for field in ("stop_id", "occupancy_status", "congestion_level", "current_status"):
        assert fields[field] == {"populated": 0, "total": 8}


def test_poll_series_orders_oldest_first(repo):
    rows = repo.poll_series(NOW - 1000)
    assert [r["polled_at"] for r in rows] == [NOW - 300, NOW - 270, NOW - 240]


def test_poll_series_filters_by_since(repo):
    rows = repo.poll_series(NOW - 260)
    assert [r["polled_at"] for r in rows] == [NOW - 240]
