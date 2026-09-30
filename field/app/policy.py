"""Sync policy: decides, per item, what stays on the device and what goes to the cloud.

This is pure logic (no I/O) so it is easy to test and easy to explain. Every
decision carries plain-language reasons that the device UI shows next to the
item, e.g. "Held on device: 2 faces detected".

Actions
  sync           upload the evidence (vectors first, then the photo)
  sync_blurred   upload a copy with faces blurred on the device; the original stays local
  hold           keep on device until someone approves (privacy)
  local_only     the officer marked it private; never leaves the device
  skip_duplicate a near-identical photo is already synced; link to it instead
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from saakshi_core.taxonomy import URGENT_WORDS


@dataclass
class ItemFacts:
    faces: int = 0
    consent: bool = False             # officer confirmed people in the photo agreed to be shown
    private: bool = False             # officer marked the item as private
    duplicate_of: Optional[str] = None
    duplicate_similarity: float = 0.0
    duplicate_synced: bool = False    # the near-duplicate is already on the server
    duplicate_gap_s: float = 0.0      # time between the two captures
    new_info: bool = False            # this item carries its own note or status report
    novelty: float = 1.0              # 1 - max similarity to anything already known
    note: str = ""
    status_claim: Optional[str] = None
    has_gps: bool = True
    size_bytes: int = 0


@dataclass
class PolicySettings:
    duplicate_threshold: float = 0.95   # cosine similarity above which two photos are "the same shot"
    burst_window_s: float = 15 * 60      # a near-identical photo within this time is a repeat, not a new observation
    privacy_mode: str = "blur"           # "blur": sync a face-blurred copy; "hold": wait for approval
    metered: bool = False                # on a metered link, only urgent items send full resolution
    full_res_priority: int = 70          # items at or above this priority send full resolution on metered links
    auto_bandwidth: bool = True          # measure the link and save bandwidth automatically when it is slow
    media_budget_mb: float = 2048.0      # originals kept on the device; older synced, HQ-checked originals are released beyond this


@dataclass
class Decision:
    action: str
    priority: int
    tier: str                            # "full" or "compressed"
    reasons: List[str] = field(default_factory=list)

    @property
    def leaves_device(self) -> bool:
        return self.action in ("sync", "sync_blurred")

    def summary(self) -> str:
        return "; ".join(self.reasons)


def _urgent_words(note: str) -> List[str]:
    text = (note or "").lower()
    return [w for w in URGENT_WORDS if w in text]


def decide(item: ItemFacts, settings: Optional[PolicySettings] = None) -> Decision:
    s = settings or PolicySettings()
    reasons: List[str] = []

    # 1. The officer's own choice always wins
    if item.private:
        return Decision("local_only", 0, "compressed", ["Marked private by the officer; stays on this device"])

    # 2. Redundancy: a repeat of a shot the server already has, with nothing new in it.
    #    The same spot photographed later is a new observation (it is how before/after works),
    #    and a photo carrying its own note or status report is new information.
    near_same = bool(item.duplicate_of) and item.duplicate_similarity >= s.duplicate_threshold
    if near_same and item.duplicate_synced and item.duplicate_gap_s <= s.burst_window_s and not item.new_info:
        pct = round(item.duplicate_similarity * 100)
        return Decision(
            "skip_duplicate",
            0,
            "compressed",
            [f"{pct}% match to a photo taken {_ago(item.duplicate_gap_s)} and already synced; linked to it instead of uploading again"],
        )

    # 3. Priority: urgent notes, bad-news status claims and novel scenes go first
    priority = 40
    urgent = _urgent_words(item.note)
    if urgent:
        priority += 35
        reasons.append(f"Urgent note ({', '.join(urgent[:3])}): sent first")
    if item.status_claim in ("damaged", "needs_attention"):
        priority += 20
        reasons.append(f"Site reported as {item.status_claim.replace('_', ' ')}")
    novelty_bonus = int(round(max(0.0, min(1.0, item.novelty)) * 20))
    priority += novelty_bonus
    if near_same and item.duplicate_gap_s > s.burst_window_s:
        reasons.append(f"Same spot as a photo from {_ago(item.duplicate_gap_s)} earlier: kept as a new observation for before/after")
    elif near_same and item.new_info:
        reasons.append("Similar to a synced photo, but carries a new note or status report")
    elif item.novelty >= 0.5:
        reasons.append("New scene for this device's memory")
    elif item.duplicate_of:
        reasons.append(f"Similar to an earlier photo ({round(item.duplicate_similarity * 100)}% match)")
    if not item.has_gps:
        priority -= 10
        reasons.append("No GPS in the photo; will need a manual location")
    priority = max(1, min(100, priority))

    # 4. Privacy: faces without consent never leave the device unblurred
    action = "sync"
    if item.faces > 0 and not item.consent:
        people = f"{item.faces} face{'s' if item.faces != 1 else ''} detected"
        if s.privacy_mode == "hold":
            return Decision("hold", priority, "compressed", [f"Held on device: {people}, no consent recorded"] + reasons)
        action = "sync_blurred"
        reasons.insert(0, f"{people}: faces blurred on the device before upload")
    elif item.faces > 0 and item.consent:
        reasons.append("Faces present, consent recorded")

    # 5. Bandwidth: on metered links only high-priority items go at full resolution
    tier = "full"
    if s.metered and priority < s.full_res_priority:
        tier = "compressed"
        reasons.append("Metered connection: compressed copy now")

    if not reasons:
        reasons.append("Routine evidence: queued for upload")
    return Decision(action, priority, tier, reasons)


def _ago(seconds: float) -> str:
    seconds = abs(seconds)
    if seconds < 90:
        return f"{int(seconds)} s"
    if seconds < 5400:
        return f"{int(seconds // 60)} min"
    if seconds < 172800:
        return f"{seconds / 3600:.0f} h"
    return f"{seconds / 86400:.0f} days"
