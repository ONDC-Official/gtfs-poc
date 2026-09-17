"""Analytics tier: the read side.

Turns the grid and route rollups into things a map can draw. Everything here
reads `analytics_*` tables, never the raw observation log - the one exception
is a single-route corridor, which is small enough to compute on demand.

The grid is returned in a compact columnar form rather than GeoJSON: 8k cells
as GeoJSON polygons is ~2 MB of mostly repeated coordinates and punctuation,
while the same cells as index rows are ~300 KB. The client reconstitutes the
rectangles from the grid origin, which it has to know anyway to line up with
the aggregator.
"""
import time
from typing import Any, Dict, List, Optional, Sequence

from ..data.repository import Repository
from .aggregator import GRID_LAT_DEG, GRID_LON_DEG, GRID_M

MPS_TO_KMH = 3.6

# Column order for the compact cell rows. Kept in one place because the client
# indexes into these positions.
CELL_COLUMNS = ["gy", "gx", "obs", "peak_vehicles", "moving", "stopped",
                "speed_kmh", "dwell_pct"]

# A cell with a handful of observations is noise, not a pattern; showing it
# makes the map look busy and the extremes meaningless.
MIN_OBS_DEFAULT = 8
MIN_OBS_HOTSPOT = 40

# A depot averages a few km/h because buses are manoeuvring or parked, not
# because the road is blocked. Congestion is only meaningful where most
# observations are through traffic, so cells that mostly sit are excluded from
# the congestion ranking - they are exactly what the dwell ranking is for.
MAX_DWELL_FOR_CONGESTION_PCT = 35.0

# Ranking cells by ascending speed always returns ten of them, even at 3am
# when the network is empty and the "slowest" cell is doing 34 km/h. That is
# not congestion, and presenting it as such teaches the reader to distrust the
# panel. A cell has to actually be slow to qualify.
CONGESTED_KMH = 15.0


class SpatialAnalytics:
    def __init__(self, repo: Repository):
        self.repo = repo

    # ---- helpers ----------------------------------------------------------
    @staticmethod
    def _since(hours: float) -> int:
        return int(time.time() - hours * 3600)

    @staticmethod
    def _row(c: Dict[str, Any]) -> List[Any]:
        obs = c["observations"] or 0
        n = c["speed_n"] or 0
        speed = round((c["speed_sum"] / n) * MPS_TO_KMH, 1) if n else None
        dwell = round((c["stopped"] or 0) / obs * 100, 1) if obs else None
        return [c["gy"], c["gx"], obs, c["peak_vehicles"] or 0,
                c["moving"] or 0, c["stopped"] or 0, speed, dwell]

    # ---- the grid ---------------------------------------------------------
    def grid(self, hours: float = 6, factor: int = 1,
             bbox: Optional[Sequence[float]] = None,
             min_observations: int = MIN_OBS_DEFAULT,
             limit: int = 20000) -> Dict[str, Any]:
        since = self._since(hours)
        cells = self.repo.grid_cells(since, factor, bbox, min_observations, limit)
        rows = [self._row(c) for c in cells]

        speeds = [r[6] for r in rows if r[6] is not None]
        obs = [r[2] for r in rows]
        return {
            "grid": {
                "lat_deg": GRID_LAT_DEG, "lon_deg": GRID_LON_DEG,
                "factor": factor, "cell_m": int(GRID_M * factor),
            },
            "window": {"since": since, "hours": hours},
            "columns": CELL_COLUMNS,
            "cells": rows,
            "count": len(rows),
            "truncated": len(rows) >= limit,
            # Ranges for the legend, so the client scales without a second pass.
            "scales": {
                "speed_kmh": {"min": min(speeds), "max": max(speeds),
                              "p10": _pct(speeds, 0.10), "p90": _pct(speeds, 0.90)}
                             if speeds else None,
                "obs": {"min": min(obs), "max": max(obs),
                        "p90": _pct(obs, 0.90)} if obs else None,
            },
        }

    # ---- hotspots ---------------------------------------------------------
    def hotspots(self, hours: float = 6, limit: int = 12,
                 min_observations: int = MIN_OBS_HOTSPOT) -> Dict[str, Any]:
        """Worst cells by congestion and by dwell, each labelled with the
        nearest scheduled stop so the row reads as a place, not a coordinate."""
        since = self._since(hours)
        cells = self.repo.grid_cells(since, 1, None, min_observations, 20000)

        enriched = []
        for c in cells:
            n = c["speed_n"] or 0
            obs = c["observations"] or 0
            if not n or not obs:
                continue
            enriched.append({
                "gy": c["gy"], "gx": c["gx"],
                "lat": (c["gy"] + 0.5) * GRID_LAT_DEG,
                "lon": (c["gx"] + 0.5) * GRID_LON_DEG,
                "observations": obs,
                "peak_vehicles": c["peak_vehicles"] or 0,
                "speed_kmh": round(c["speed_sum"] / n * MPS_TO_KMH, 1),
                "dwell_pct": round((c["stopped"] or 0) / obs * 100, 1),
            })

        through = [c for c in enriched
                   if c["dwell_pct"] <= MAX_DWELL_FOR_CONGESTION_PCT
                   and c["speed_kmh"] <= CONGESTED_KMH]
        congestion = sorted(through, key=lambda c: c["speed_kmh"])[:limit]
        dwell = sorted(enriched, key=lambda c: -c["dwell_pct"])[:limit]
        busiest = sorted(enriched, key=lambda c: -c["observations"])[:limit]
        for group in (congestion, dwell, busiest):
            for c in group:
                self._label(c)
        return {
            "window": {"since": since, "hours": hours},
            "min_observations": min_observations,
            "max_dwell_for_congestion_pct": MAX_DWELL_FOR_CONGESTION_PCT,
            "congested_kmh": CONGESTED_KMH,
            "congestion": congestion,
            "dwell": dwell,
            "busiest": busiest,
        }

    def _label(self, cell: Dict[str, Any]) -> None:
        stop = self.repo.nearest_stop(cell["lat"], cell["lon"], 0.01)
        cell["place"] = stop["stop_name"] if stop else None
        cell["stop_id"] = stop["stop_id"] if stop else None

    # ---- one corridor -----------------------------------------------------
    def corridor(self, route_id: str, hours: float = 6) -> Dict[str, Any]:
        since = self._since(hours)
        cells = self.repo.route_corridor(route_id, since)
        points = []
        for c in cells:
            n = c["speed_n"] or 0
            obs = c["observations"] or 0
            points.append({
                "lat": round(c["lat"], 5), "lon": round(c["lon"], 5),
                "observations": obs,
                "speed_kmh": round(c["speed_sum"] / n * MPS_TO_KMH, 1) if n else None,
                "dwell_pct": round((c["stopped"] or 0) / obs * 100, 1) if obs else None,
            })
        speeds = [p["speed_kmh"] for p in points if p["speed_kmh"] is not None]
        return {
            "route_id": route_id,
            "window": {"since": since, "hours": hours},
            "cell_m": int(GRID_M),
            "points": points,
            "summary": {
                "cells": len(points),
                "observations": sum(p["observations"] for p in points),
                "avg_speed_kmh": round(sum(speeds) / len(speeds), 1) if speeds else None,
                "slowest_kmh": min(speeds) if speeds else None,
                "fastest_kmh": max(speeds) if speeds else None,
            },
        }

    # ---- time series ------------------------------------------------------
    def hourly(self, hours: float = 24) -> Dict[str, Any]:
        since = self._since(hours)
        out = []
        for r in self.repo.hourly_series(since):
            n = r["speed_n"] or 0
            out.append({
                "hour": r["hour_bucket"],
                "observations": r["observations"],
                "routes": r["routes"],
                "moving": r["moving"],
                "stopped": r["stopped"],
                "avg_speed_kmh": round(r["speed_sum"] / n * MPS_TO_KMH, 1) if n else None,
                "dwell_pct": (round(r["stopped"] / r["observations"] * 100, 1)
                              if r["observations"] else None),
            })
        return {"window": {"since": since, "hours": hours}, "items": out}


def _pct(values: List[float], p: float):
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(len(s) * p))]
