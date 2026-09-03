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
    def vehicle_history(self, vehicle_id: str, since_ts: int) -> List[Dict[str, Any]]: ...

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
