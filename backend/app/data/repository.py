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
