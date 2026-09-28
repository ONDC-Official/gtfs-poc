"""Matches a live trip to the nearest timetabled departure on its route.

A live trip_id (`1706_15_23_4361`) is never equal to a static one
(`1706_15_23`): the live one encodes the vehicle's *actual* dispatch time
plus a running sequence number, and the feed marks every trip
`schedule_relationship=ADDED` - the provider's own way of saying none of
these are scheduled trips. So "is this trip on the timetable" can't be a
column equality (or a FOREIGN KEY, see live_trip_match's schema comment);
it has to be computed: same route, nearest scheduled departure, within a
tolerance.

`route_HH_MM[_seq]` for both live and static trip_ids - `_seq` only appears
on the live side.
"""
import bisect
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from ..data.repository import Repository

# A live dispatch within this many minutes of a timetabled departure on the
# same route counts as that departure. Matches the tolerance used throughout
# the quality tier's own schedule-adherence checks.
RELAXED_TOLERANCE_MIN = 15


def parse_dispatch_minute(trip_id: str) -> Optional[int]:
    """`route_HH_MM...` -> HH*60+MM (minutes since local midnight), or None if
    trip_id doesn't have this shape at all. HH is allowed up to 47 so a
    post-midnight service written against the previous day's departures
    (GTFS's own convention for `>24:00:00` times) still parses."""
    parts = trip_id.split("_")
    if len(parts) < 3:
        return None
    try:
        hh, mm = int(parts[1]), int(parts[2])
    except ValueError:
        return None
    if not (0 <= hh <= 47 and 0 <= mm < 60):
        return None
    return hh * 60 + mm


@dataclass(frozen=True)
class MatchResult:
    matched_trip_id: Optional[str]
    match_type: str  # "strict" | "relaxed" | "none"
    delta_minutes: Optional[float]


class TripMatcher:
    """Holds the static timetable's departures indexed by route and sorted by
    minute-of-day, so matching one live trip is a bisect, not a query. The
    index is small (as many rows as gtfs_trips - tens of thousands) and static
    data changes rarely, so it's loaded once and reused; call `refresh()`
    after a static reload."""

    def __init__(self, repo: Repository):
        self.repo = repo
        self._by_route: Dict[str, List[Tuple[int, str]]] = {}
        self._loaded = False

    def refresh(self) -> None:
        by_route: Dict[str, List[Tuple[int, str]]] = {}
        for route_id, trip_id in self.repo.scheduled_trip_keys():
            minute = parse_dispatch_minute(trip_id)
            if minute is None:
                continue
            by_route.setdefault(route_id, []).append((minute, trip_id))
        for entries in by_route.values():
            entries.sort()
        self._by_route = by_route
        self._loaded = True

    def match(self, route_id: str, live_trip_id: str) -> MatchResult:
        if not self._loaded:
            self.refresh()
        minute = parse_dispatch_minute(live_trip_id)
        if minute is None:
            return MatchResult(None, "none", None)
        candidates = self._by_route.get(route_id)
        if not candidates:
            return MatchResult(None, "none", None)

        minutes = [m for m, _ in candidates]
        i = bisect.bisect_left(minutes, minute)
        best: Optional[Tuple[int, str]] = None
        for j in (i - 1, i):
            if 0 <= j < len(candidates):
                cand_minute, cand_trip = candidates[j]
                delta = abs(cand_minute - minute)
                if best is None or delta < best[0]:
                    best = (delta, cand_trip)
        if best is None:
            return MatchResult(None, "none", None)

        delta, sched_trip_id = best
        if delta == 0:
            return MatchResult(sched_trip_id, "strict", 0.0)
        if delta <= RELAXED_TOLERANCE_MIN:
            return MatchResult(sched_trip_id, "relaxed", float(delta))
        return MatchResult(None, "none", float(delta))
