"""Evidence integrity: can a funder trust this photo?

Pure function over facts gathered during ingest. Every check returns a status
(pass / warn / fail / info) and a sentence a programme manager understands.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from saakshi_core.projects import Project


@dataclass
class Check:
    key: str
    status: str   # pass | warn | fail | info
    message: str


@dataclass
class ReuseMatch:
    asset_id: str
    project_id: str
    project_name: str
    site_id: Optional[str]
    captured_at: Optional[str]
    how: str          # "same file", "near-identical image"
    distance: float   # hamming bits or 1 - cosine


@dataclass
class IntegrityInput:
    has_exif: bool
    has_gps: bool
    lat: Optional[float]
    lon: Optional[float]
    captured_at: Optional[str]
    uploaded_at: str
    edited_with: Optional[str]
    quality: Optional[float]           # Cloudinary quality_analysis focus, 0..1
    faces: int
    consent: bool
    project: Optional[Project]
    site_id: Optional[str]
    reuse: List[ReuseMatch] = field(default_factory=list)
    is_video: bool = False


@dataclass
class Integrity:
    score: int
    level: str   # verified | review | flagged
    checks: List[Check]

    def to_dict(self) -> Dict[str, Any]:
        return {"score": self.score, "level": self.level, "checks": [asdict(c) for c in self.checks]}


def _days_between(a: str, b: str) -> Optional[float]:
    try:
        da = datetime.fromisoformat(a.replace("Z", "+00:00")).replace(tzinfo=None)
        db = datetime.fromisoformat(b.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None
    return (db - da).total_seconds() / 86400.0


def score(x: IntegrityInput, stale_days: float = 30.0) -> Integrity:
    checks: List[Check] = []

    # Camera metadata
    if x.has_exif:
        checks.append(Check("exif", "pass", "Camera metadata present"))
    elif x.is_video:
        checks.append(Check("exif", "warn", "No capture metadata in the video file; ask for the original recording"))
    else:
        checks.append(Check("exif", "warn", "No camera metadata. Messaging apps like WhatsApp strip it; ask for the original file"))

    # Location
    p = x.project
    if not x.has_gps or x.lat is None or x.lon is None:
        checks.append(Check("gps", "warn", "No GPS location in the photo"))
    else:
        checks.append(Check("gps", "pass", "GPS location present"))
        if p is not None:
            dist_km = p.distance_m(x.lat, x.lon) / 1000.0
            if p.contains(x.lat, x.lon):
                checks.append(Check("in_area", "pass", f"Taken inside the project area ({dist_km:.1f} km from its centre)"))
            else:
                over = dist_km - p.radius_m / 1000.0
                checks.append(Check("in_area", "fail", f"Taken {over:.1f} km outside the project area"))

    # Time
    if x.captured_at and p is not None:
        day = x.captured_at[:10]
        if day < p.start_date:
            checks.append(Check("in_window", "fail", f"Taken on {day}, before the project started ({p.start_date})"))
        elif day > p.end_date:
            checks.append(Check("in_window", "warn", f"Taken on {day}, after the project ended ({p.end_date})"))
        else:
            checks.append(Check("in_window", "pass", f"Taken on {day}, within the project dates"))
        gap = _days_between(x.captured_at, x.uploaded_at)
        if gap is not None and gap > stale_days:
            checks.append(Check("fresh", "warn", f"Submitted {gap:.0f} days after it was taken"))
    elif not x.captured_at:
        checks.append(Check("in_window", "warn", "No capture date in the photo"))

    # Editing
    if x.edited_with:
        checks.append(Check("edited", "warn", f"Saved by editing software: {x.edited_with}"))

    # Reuse across submissions
    worst = None
    for m in x.reuse:
        if m.project_id != (p.id if p else None):
            st = "fail"
            msg = f"{'Same file' if m.how == 'same file' else 'Near-identical photo'} already submitted for {m.project_name}"
        elif m.site_id and x.site_id and m.site_id != x.site_id:
            st = "warn"
            msg = f"{'Same file' if m.how == 'same file' else 'Near-identical photo'} already used for another site in this project"
        elif m.how == "same file":
            st = "warn"
            msg = "This exact file was already submitted for this site"
        else:
            st = "info"
            msg = "Repeat shot of the same site"
        if m.captured_at:
            msg += f" (taken {m.captured_at[:10]})"
        rank = {"fail": 3, "warn": 2, "info": 1}[st]
        if worst is None or rank > worst[0]:
            worst = (rank, Check("reuse", st, msg))
    if worst:
        checks.append(worst[1])
    else:
        checks.append(Check("reuse", "pass", "Not seen in any earlier submission"))

    # Quality
    if x.quality is not None:
        if x.quality < 0.35:
            checks.append(Check("quality", "warn", f"Photo looks blurry (focus score {x.quality:.2f})"))
        else:
            checks.append(Check("quality", "pass", f"Sharp enough to use (focus score {x.quality:.2f})"))

    # People
    if x.faces:
        if x.consent:
            checks.append(Check("faces", "info", f"{x.faces} face(s); consent recorded"))
        else:
            checks.append(Check("faces", "info", f"{x.faces} face(s); blurred automatically in reports and campaign images"))

    total = 100
    for c in checks:
        if c.status == "fail":
            total -= 50 if c.key == "reuse" else 35
        elif c.status == "warn":
            total -= 10
    total = max(0, min(100, total))
    fails = sum(1 for c in checks if c.status == "fail")
    warns = sum(1 for c in checks if c.status == "warn")
    if fails or total < 50:
        level = "flagged"
    elif total < 80 or warns >= 2:
        level = "review"
    else:
        level = "verified"
    return Integrity(total, level, checks)
