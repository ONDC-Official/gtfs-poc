"""Small geometry helpers shared across the analytics/quality services."""
import math
from typing import List, Optional, Sequence, Tuple


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    mlat = math.radians((lat1 + lat2) / 2.0)
    dy = math.radians(lat2 - lat1) * 6371000.0
    dx = math.radians(lon2 - lon1) * 6371000.0 * math.cos(mlat)
    return math.hypot(dx, dy)


def point_to_polyline_m(lat: float, lon: float,
                        polyline: Sequence[Sequence[float]]) -> Optional[float]:
    """Shortest distance from (lat, lon) to a polyline of [lon, lat] points
    (the shape of `Repository.route_shape`), in metres.

    Projects each segment onto an equirectangular plane local to the point -
    exact enough at city scale and simple enough to run per-vehicle without a
    spatial index. Returns None for a degenerate (empty or single-point) line.
    """
    if len(polyline) < 2:
        return None
    coslat = max(math.cos(math.radians(lat)), 1e-9)

    def to_xy(lon_p: float, lat_p: float) -> Tuple[float, float]:
        return ((lon_p - lon) * 111_320.0 * coslat, (lat_p - lat) * 111_320.0)

    px, py = 0.0, 0.0  # the point itself, in its own local frame
    best = None
    ax, ay = to_xy(*polyline[0])
    for bx_lon, by_lat in polyline[1:]:
        bx, by = to_xy(bx_lon, by_lat)
        dx, dy = bx - ax, by - ay
        seg_len2 = dx * dx + dy * dy
        if seg_len2 <= 1e-9:
            t = 0.0
        else:
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg_len2))
        cx, cy = ax + t * dx, ay + t * dy
        d = math.hypot(px - cx, py - cy)
        if best is None or d < best:
            best = d
        ax, ay = bx, by
    return best
