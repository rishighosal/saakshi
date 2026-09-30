"""Small geo helpers. No external dependencies so they run anywhere."""

from __future__ import annotations

import math
from typing import Iterable, Optional, Tuple

EARTH_RADIUS_M = 6_371_000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def nearest(
    lat: float, lon: float, candidates: Iterable[Tuple[str, float, float]]
) -> Optional[Tuple[str, float]]:
    """Return (id, distance_m) of the closest candidate, or None if there are none."""
    best: Optional[Tuple[str, float]] = None
    for cid, clat, clon in candidates:
        d = haversine_m(lat, lon, clat, clon)
        if best is None or d < best[1]:
            best = (cid, d)
    return best


def short_coord(lat: float, lon: float) -> str:
    """Human-friendly coordinate label, e.g. '22.5726°N 88.3639°E'."""
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"{abs(lat):.4f}°{ns} {abs(lon):.4f}°{ew}"


def site_key(lat: float, lon: float, precision: int = 3) -> str:
    """Stable id for an auto-created site (~110 m cells at precision 3)."""
    return f"site_{lat:.{precision}f}_{lon:.{precision}f}"
