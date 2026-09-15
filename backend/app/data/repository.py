"""The data-plane port.

The service layer depends only on this interface. `SqliteRepository` is the
one implementation today; a Postgres/PostGIS or DuckDB implementation for the
analytics tier plugs in here without any service-layer change.
"""
import abc
from typing import Any, Dict, List, Optional, Sequence


class Repository(abc.ABC):
    # ---- static -----------------------------------------------------------
    @abc.abstractmethod
    def feed_summary(self) -> Dict[str, Any]:
        """Row counts + bounding box for the loaded static feed."""

    @abc.abstractmethod
    def static_feed_meta(self) -> Dict[str, Any]:
        """Everything `feed_summary` returns except `history_rows`: that field
        is a COUNT(*) over the (unbounded, constantly-written) realtime history
        table, expensive enough under a live poller that a caller which only
        wants the static counts and bbox - the quality tier, notably - should
        not have to pay for it."""

    @abc.abstractmethod
    def list_routes(self, q: Optional[str], limit: int, offset: int) -> Dict[str, Any]: ...

    @abc.abstractmethod
    def get_route(self, route_id: str) -> Optional[Dict[str, Any]]: ...

    @abc.abstractmethod
    def route_shape(self, route_id: str) -> List[List[float]]:
        """The longest shape for the route, as [[lon, lat], ...]."""

    @abc.abstractmethod
    def route_stops(self, route_id: str) -> List[Dict[str, Any]]: ...

    @abc.abstractmethod
    def list_stops(self, q: Optional[str], bbox: Optional[Sequence[float]],
                   limit: int) -> List[Dict[str, Any]]: ...

    @abc.abstractmethod
    def stop_schedule(self, stop_id: str, from_s: int, to_s: int,
                      limit: int) -> List[Dict[str, Any]]: ...

    @abc.abstractmethod
    def routes_nearby(self, lat: float, lon: float, radius_m: float,
                      limit: int) -> List[Dict[str, Any]]:
        """Routes serving any stop within `radius_m` of the point, nearest
        first. Each entry carries the names of the stops that put it in range
        and the distance to the closest of them."""

    @abc.abstractmethod
    def route_names(self, route_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        """Bulk route lookup used to decorate realtime vehicles."""

    # ---- realtime ---------------------------------------------------------
    @abc.abstractmethod
    def upsert_vehicles(self, rows: List[Dict[str, Any]]) -> int:
        """Write an observation batch. Returns rows newly added to history."""

    @abc.abstractmethod
    def latest_vehicles(self, route_id: Optional[str],
                        bbox: Optional[Sequence[float]],
                        max_age_s: Optional[int]) -> List[Dict[str, Any]]: ...

    @abc.abstractmethod
    def vehicle_history(self, vehicle_id: str, since_ts: int,
                        until_ts: Optional[int] = None) -> List[Dict[str, Any]]:
        """Observations for one vehicle, oldest first. `until_ts` bounds the
        window at the top; None means "up to the newest row"."""

    @abc.abstractmethod
    def log_poll(self, **kwargs: Any) -> None: ...

    @abc.abstractmethod
    def recent_polls(self, limit: int) -> List[Dict[str, Any]]: ...

    @abc.abstractmethod
    def prune_history(self, older_than_ts: int) -> int: ...

    @abc.abstractmethod
    def purge_simulated(self) -> int:
        """Drop rows left behind by the mock feed. Called when a real feed is
        configured, so simulated buses never mix into live counts."""

    @abc.abstractmethod
    def route_count(self) -> int:
        """Routes in the static schedule - the denominator for coverage."""

    @abc.abstractmethod
    def dark_routes(self, limit: int) -> List[Dict[str, Any]]:
        """Scheduled routes with no vehicle currently reporting."""

    # ---- analytics --------------------------------------------------------
    @abc.abstractmethod
    def fleet_stats(self, window_s: int) -> Dict[str, Any]: ...

    @abc.abstractmethod
    def active_routes(self, limit: int) -> List[Dict[str, Any]]: ...

    @abc.abstractmethod
    def ingest_timeseries(self, bucket_s: int, since_ts: int) -> List[Dict[str, Any]]: ...

    @abc.abstractmethod
    def speed_histogram(self, since_ts: int, buckets: int) -> List[Dict[str, Any]]: ...

    # ---- analytics tier (rollups) -----------------------------------------
    @abc.abstractmethod
    def grid_cells(self, since_ts: int, factor: int,
                   bbox: Optional[Sequence[float]], min_observations: int,
                   limit: int) -> List[Dict[str, Any]]:
        """Grid rollup aggregated to `factor` x the base cell size."""

    @abc.abstractmethod
    def grid_extent(self, since_ts: int) -> Dict[str, Any]:
        """Value ranges over the whole grid, so a map legend can be scaled
        without shipping every cell to the client."""

    @abc.abstractmethod
    def nearest_stop(self, lat: float, lon: float,
                     radius_deg: float) -> Optional[Dict[str, Any]]:
        """Nearest scheduled stop, used to give a grid cell a human label."""

    @abc.abstractmethod
    def route_corridor(self, route_id: str, since_ts: int) -> List[Dict[str, Any]]:
        """Per-cell speeds for one route, straight from the observation log."""

    @abc.abstractmethod
    def hourly_series(self, since_ts: int) -> List[Dict[str, Any]]: ...

    # ---- analytics tier (the rollup write path) ---------------------------
    # The aggregator owns the *policy* - watermark, rewind, hour alignment.
    # These are the storage primitives it drives, so that no service holds a
    # SQL dialect.
    @abc.abstractmethod
    def meta_get(self, key: str) -> Optional[str]:
        """Read a meta value, or None when the key was never written."""

    @abc.abstractmethod
    def meta_set(self, key: str, value: str) -> None:
        """Upsert a meta value."""

    @abc.abstractmethod
    def newest_observation_ts(self) -> Optional[int]:
        """MAX(ts) over the observation log; None when the log is empty."""

    @abc.abstractmethod
    def count_observations_since(self, since_ts: int) -> int: ...

    @abc.abstractmethod
    def fold_rollups(self, since_ts: int, lat_deg: float, lon_deg: float,
                     hour_s: int, moving_mps: float, cap_mps: float) -> None:
        """Recompute every rollup bucket at or after `since_ts` from the log.

        Replaces whole buckets rather than accumulating deltas - that is what
        makes a re-run over an already-folded window idempotent. Must be one
        transaction: a half-applied fold would leave the rollups short until
        the next pass.
        """

    @abc.abstractmethod
    def rollup_counts(self) -> Dict[str, int]:
        """Row counts of the rollup tables, as {"cells": n, "routes": n}."""

    # ---- quality tier -------------------------------------------------------
    @abc.abstractmethod
    def route_coverage_by_hour(self, since_ts: int) -> List[Dict[str, Any]]:
        """Distinct live routes per (hour-of-day, is_weekend) bucket, from the
        route-hour rollup. Layer 1.5 temporal coverage."""

    @abc.abstractmethod
    def distinct_grid_cells(self, since_ts: int) -> int:
        """Count of base-resolution grid cells with >=1 observation since
        `since_ts`. Layer 1.4 spatial coverage numerator."""

    @abc.abstractmethod
    def continuity_report(self, since_ts: int, until_ts: int,
                          gap_threshold_s: int) -> Dict[str, Any]:
        """Per-vehicle reporting-gap stats over [since_ts, until_ts] from the
        observation log: report continuity ratio, gap count/median/p95, and a
        churn count of gaps that exceed `gap_threshold_s` (i.e. the vehicle
        went stale and came back). Layers 3.1, 3.2, 3.4."""

    @abc.abstractmethod
    def trip_completeness(self, since_ts: int, until_ts: int,
                          gap_threshold_s: int) -> Dict[str, Any]:
        """Same gap analysis as `continuity_report`, but partitioned by
        (vehicle_id, trip_id): the share of trip segments with no internal
        gap over `gap_threshold_s`. Layer 3.3."""

    @abc.abstractmethod
    def referential_integrity(self, since_ts: int) -> Dict[str, Any]:
        """Counts of live vehicles whose route_id/trip_id do not exist in the
        static schedule. Layer 4.4."""

    @abc.abstractmethod
    def field_population(self, since_ts: int) -> Dict[str, Dict[str, int]]:
        """Per-field {populated, total} counts over recent history, for every
        optional GTFS-realtime field. Layer 5.1."""

    @abc.abstractmethod
    def poll_series(self, since_ts: int) -> List[Dict[str, Any]]:
        """Poll log rows since `since_ts`, oldest first - a time-bounded
        counterpart to `recent_polls` for volume-stability and error-breakdown
        stats that need a full window rather than a row count. Layers 6.2, 6.3."""
