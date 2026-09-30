"""Conflicting information about a site, from different devices.

Each photo can carry a status claim about its site ("completed", "damaged",
...). Devices capture claims offline, so two officers can report different
things about the same site. The rule:

  * same status                 -> agree, keep the newer timestamp
  * different, far apart in time -> newer evidence wins (the site changed)
  * different, close in time     -> genuine conflict; a person decides

"Close in time" is the conflict window (default 6 hours).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Claim:
    site_id: str
    status: str
    ts: float
    device_id: str
    evidence_id: str


@dataclass
class Outcome:
    kind: str            # "new", "same", "newer_wins", "older_ignored", "conflict"
    message: str


def evaluate(current: Optional[Claim], incoming: Claim, window_s: float = 6 * 3600) -> Outcome:
    if current is None:
        return Outcome("new", f"First status for this site: {incoming.status}")
    if current.evidence_id == incoming.evidence_id:
        return Outcome("same", "Same evidence")
    if current.status == incoming.status:
        return Outcome("same", f"Both reports agree: {incoming.status}")
    gap = incoming.ts - current.ts
    if abs(gap) <= window_s and current.device_id != incoming.device_id:
        hours = abs(gap) / 3600
        return Outcome(
            "conflict",
            f"{current.device_id} says {current.status}, {incoming.device_id} says {incoming.status}, {hours:.1f} h apart",
        )
    if gap > 0:
        return Outcome("newer_wins", f"Newer report ({incoming.status}) replaces {current.status}")
    return Outcome("older_ignored", f"Older report ({incoming.status}) kept in history only")
