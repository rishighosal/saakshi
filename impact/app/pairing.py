"""Before/after pairing: find two photos of the same spot, far enough apart in time.

Pure logic. Candidates are the assets of one site (sites are GPS clusters of
~150 m). The "after" is chosen among the latest photos as the one that best
matches the "before" viewpoint, using CLIP similarity when vectors exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

import numpy as np

from saakshi_core.taxonomy import PROBLEM_TAGS, PROGRESS_TAGS, label


@dataclass
class PairCandidate:
    id: str
    captured_ts: float
    tags: Sequence[str]
    vector: Optional[Sequence[float]] = None
    level: str = "verified"


@dataclass
class Pair:
    before_id: str
    after_id: str
    gap_days: float
    similarity: Optional[float]
    added: List[str]
    removed: List[str]
    verdict: str        # improved | declined | changed | unclear
    summary: str


def _cos(a: Optional[Sequence[float]], b: Optional[Sequence[float]]) -> Optional[float]:
    if a is None or b is None:
        return None
    va, vb = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
    if not na or not nb:
        return None
    return float(va @ vb / (na * nb))


def tag_delta(before: Sequence[str], after: Sequence[str]) -> Dict[str, List[str]]:
    b, a = set(before or []), set(after or [])
    return {"added": sorted(a - b), "removed": sorted(b - a)}


def verdict(added: Sequence[str], removed: Sequence[str]) -> str:
    good = len([t for t in removed if t in PROBLEM_TAGS]) + len([t for t in added if t in PROGRESS_TAGS])
    bad = len([t for t in added if t in PROBLEM_TAGS]) + len([t for t in removed if t in PROGRESS_TAGS])
    if good > bad:
        return "improved"
    if bad > good:
        return "declined"
    if added or removed:
        return "changed"
    return "unclear"


def describe(added: Sequence[str], removed: Sequence[str], v: str) -> str:
    parts = []
    if removed:
        parts.append("no longer shows " + ", ".join(label(t).lower() for t in removed))
    if added:
        parts.append("now shows " + ", ".join(label(t).lower() for t in added))
    if not parts:
        return "No change in detected content between the two photos"
    lead = {"improved": "Improvement", "declined": "Deterioration", "changed": "Change"}.get(v, "Change")
    return f"{lead}: the site " + " and ".join(parts)


def best_pair(
    items: Sequence[PairCandidate],
    min_gap_hours: float = 1.0,
    min_similarity: float = 0.55,
    after_pool: int = 3,
) -> Optional[Pair]:
    """Pick the earliest photo as 'before' and the best-matching late photo as 'after'."""
    usable = [i for i in items if i.level != "flagged"]
    if len(usable) < 2:
        return None
    usable = sorted(usable, key=lambda i: i.captured_ts)
    before = usable[0]
    late = [i for i in usable[1:] if (i.captured_ts - before.captured_ts) >= min_gap_hours * 3600]
    if not late:
        return None
    pool = late[-after_pool:]
    scored = [(i, _cos(before.vector, i.vector)) for i in pool]
    with_sim = [(i, s) for i, s in scored if s is not None]
    if with_sim:
        after, sim = max(with_sim, key=lambda t: t[1])
        if sim < min_similarity:
            # Different viewpoints: fall back to the latest photo but keep the low similarity visible
            after, sim = pool[-1], _cos(before.vector, pool[-1].vector)
    else:
        after, sim = pool[-1], None
    d = tag_delta(before.tags, after.tags)
    v = verdict(d["added"], d["removed"])
    return Pair(
        before_id=before.id,
        after_id=after.id,
        gap_days=round((after.captured_ts - before.captured_ts) / 86400.0, 2),
        similarity=round(sim, 4) if sim is not None else None,
        added=d["added"],
        removed=d["removed"],
        verdict=v,
        summary=describe(d["added"], d["removed"], v),
    )
