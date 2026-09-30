"""Projects and sites: the frame every piece of evidence hangs on.

The cloud app owns the project list. Devices cache it and fall back to the
bundled demo file when they have never been online.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

from .geo import haversine_m, short_coord, site_key

BUNDLED = Path(__file__).parent / "data" / "projects.json"

# A photo within this distance of a known site belongs to that site
SITE_RADIUS_M = 150.0


@dataclass
class Site:
    id: str
    name: str
    lat: float
    lon: float
    project_id: str = ""
    auto: bool = False


@dataclass
class Project:
    id: str
    name: str
    activity: str
    description: str
    lat: float
    lon: float
    radius_m: float
    start_date: str
    end_date: str
    sites: List[Site] = field(default_factory=list)

    def contains(self, lat: float, lon: float) -> bool:
        return haversine_m(self.lat, self.lon, lat, lon) <= self.radius_m

    def distance_m(self, lat: float, lon: float) -> float:
        return haversine_m(self.lat, self.lon, lat, lon)

    def in_window(self, iso_day: Optional[str]) -> Optional[bool]:
        if not iso_day:
            return None
        day = iso_day[:10]
        return self.start_date <= day <= self.end_date

    def to_dict(self) -> Dict:
        d = asdict(self)
        return d


def load_bundled() -> Dict:
    """The demo projects, or another file named by SAAKSHI_PROJECTS_FILE (the tests use their own)."""
    path = Path(os.environ.get("SAAKSHI_PROJECTS_FILE") or BUNDLED)
    return json.loads(path.read_text(encoding="utf-8"))


def parse_projects(data: Dict) -> List[Project]:
    out: List[Project] = []
    for p in data.get("projects", []):
        sites = [Site(project_id=p["id"], **{k: s[k] for k in ("id", "name", "lat", "lon")}, auto=bool(s.get("auto", False))) for s in p.get("sites", [])]
        out.append(
            Project(
                id=p["id"],
                name=p["name"],
                activity=p.get("activity", ""),
                description=p.get("description", ""),
                lat=float(p["lat"]),
                lon=float(p["lon"]),
                radius_m=float(p.get("radius_m", 5000)),
                start_date=p.get("start_date", "2000-01-01"),
                end_date=p.get("end_date", "2100-01-01"),
                sites=sites,
            )
        )
    return out


def guess_project(projects: List[Project], lat: Optional[float], lon: Optional[float]) -> Optional[Project]:
    """Pick the smallest project area that contains the point."""
    if lat is None or lon is None:
        return None
    inside = [p for p in projects if p.contains(lat, lon)]
    if not inside:
        return None
    return min(inside, key=lambda p: p.radius_m)


def assign_site(project: Project, lat: Optional[float], lon: Optional[float], extra_sites: Optional[List[Site]] = None) -> Site:
    """Nearest known site within SITE_RADIUS_M, otherwise a new auto site at this spot."""
    candidates = list(project.sites) + [s for s in (extra_sites or []) if s.project_id == project.id]
    if lat is None or lon is None:
        return Site(id=f"{project.id}-unlocated", name="No location", lat=project.lat, lon=project.lon, project_id=project.id, auto=True)
    best = None
    for s in candidates:
        d = haversine_m(lat, lon, s.lat, s.lon)
        if d <= SITE_RADIUS_M and (best is None or d < best[1]):
            best = (s, d)
    if best:
        return best[0]
    return Site(id=site_key(lat, lon), name=f"Spot {short_coord(lat, lon)}", lat=round(lat, 6), lon=round(lon, 6), project_id=project.id, auto=True)


def today_iso() -> str:
    return date.today().isoformat()
