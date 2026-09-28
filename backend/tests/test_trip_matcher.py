"""TripMatcher: pure matching logic (parse_dispatch_minute, match), no DB."""
from app.services.trip_matcher import MatchResult, TripMatcher, parse_dispatch_minute


def test_parse_dispatch_minute_ordinary():
    assert parse_dispatch_minute("1706_15_23_4361") == 15 * 60 + 23
    assert parse_dispatch_minute("1000_06_10") == 6 * 60 + 10


def test_parse_dispatch_minute_allows_post_midnight_hour():
    assert parse_dispatch_minute("29_25_05") == 25 * 60 + 5


def test_parse_dispatch_minute_rejects_malformed():
    assert parse_dispatch_minute("no_underscore") is None
    assert parse_dispatch_minute("R1_ab_cd") is None
    assert parse_dispatch_minute("R1_99_05") is None       # hour out of range
    assert parse_dispatch_minute("R1_10_75") is None        # minute out of range
    assert parse_dispatch_minute("R1") is None
    assert parse_dispatch_minute("") is None


class _FakeRepo:
    def __init__(self, static_trips):
        self._static_trips = static_trips

    def scheduled_trip_keys(self):
        return self._static_trips


def test_exact_minute_is_a_strict_match():
    m = TripMatcher(_FakeRepo([("R1", "R1_06_10")]))
    r = m.match("R1", "R1_06_10_9999")
    assert r == MatchResult("R1_06_10", "strict", 0.0)


def test_within_tolerance_is_a_relaxed_match():
    m = TripMatcher(_FakeRepo([("R1", "R1_06_10")]))
    r = m.match("R1", "R1_06_20_1")   # 10 min after the scheduled departure
    assert r.match_type == "relaxed"
    assert r.matched_trip_id == "R1_06_10"
    assert r.delta_minutes == 10.0


def test_beyond_tolerance_is_no_match():
    m = TripMatcher(_FakeRepo([("R1", "R1_06_10")]))
    r = m.match("R1", "R1_06_30_1")   # 20 min after - outside the 15 min tolerance
    assert r.match_type == "none"
    assert r.matched_trip_id is None
    # still reports how far off it was, even though it didn't match
    assert r.delta_minutes == 20.0


def test_picks_the_nearest_of_several_departures_on_the_route():
    m = TripMatcher(_FakeRepo([("R1", "R1_06_00"), ("R1", "R1_06_10"), ("R1", "R1_07_00")]))
    r = m.match("R1", "R1_06_08_1")
    assert r.matched_trip_id == "R1_06_10"
    assert r.match_type == "relaxed"
    assert r.delta_minutes == 2.0


def test_route_with_no_static_trips_at_all_is_no_match():
    m = TripMatcher(_FakeRepo([("R1", "R1_06_10")]))
    r = m.match("R9_UNSCHEDULED", "R9_UNSCHEDULED_06_10_1")
    assert r == MatchResult(None, "none", None)


def test_malformed_live_trip_id_is_no_match_without_crashing():
    m = TripMatcher(_FakeRepo([("R1", "R1_06_10")]))
    r = m.match("R1", "not-shaped-like-a-trip-id")
    assert r == MatchResult(None, "none", None)


def test_refresh_ignores_static_trips_that_do_not_parse():
    """A malformed static trip_id shouldn't crash the index build - it's
    just not a usable match target."""
    m = TripMatcher(_FakeRepo([("R1", "garbage"), ("R1", "R1_06_10")]))
    r = m.match("R1", "R1_06_10_1")
    assert r.match_type == "strict"
