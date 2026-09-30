"""Field-level merge for evidence edited on more than one device while offline.

Editable fields carry their own timestamp in `field_ts`. When two copies of
the same evidence meet, each field keeps the value that was written last
(a last-writer-wins map). Edits to different fields never overwrite each other.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

EDITABLE_FIELDS = ("note", "status_claim", "consent", "private")


def merge_payloads(local: Dict[str, Any], remote: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Merge two copies of one evidence payload.

    Returns (merged_payload, changes) where `changes` lists the fields that
    were taken from the remote copy, for the activity log.
    """
    merged = dict(local)
    lts: Dict[str, float] = dict(local.get("field_ts") or {})
    rts: Dict[str, float] = dict(remote.get("field_ts") or {})
    lby: Dict[str, str] = dict(local.get("field_by") or {})
    rby: Dict[str, str] = dict(remote.get("field_by") or {})
    taken: List[str] = []
    for f in EDITABLE_FIELDS:
        r_ts = float(rts.get(f, 0.0))
        l_ts = float(lts.get(f, 0.0))
        if r_ts > l_ts and remote.get(f) != local.get(f):
            merged[f] = remote.get(f)
            lts[f] = r_ts
            if f in rby:
                lby[f] = rby[f]
            taken.append(f)
    merged["field_ts"] = lts
    merged["field_by"] = lby
    merged["version"] = max(int(local.get("version", 1)), int(remote.get("version", 1))) + (1 if taken else 0)
    # Cloud enrichment only ever comes from the server copy
    if remote.get("cloud"):
        merged["cloud"] = remote["cloud"]
    return merged, taken
