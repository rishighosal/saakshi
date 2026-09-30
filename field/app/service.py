"""Everything the device does with evidence: import, search, edit, resolve conflicts.

Network work lives in `sync.py`; this module never touches the network, so
all of it works with the Wi-Fi off.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from saakshi_core import taxonomy
from saakshi_core.embeddings import Embedder, bm25_document, bm25_query
from saakshi_core.imaging import (
    compressed_bytes,
    dhash,
    hamming_hex,
    iso_to_ts,
    open_image,
    read_exif,
    sha256_bytes,
    thumbnail_bytes,
)
from saakshi_core.projects import Project, Site, assign_site, guess_project, load_bundled, parse_projects
from saakshi_core.schema import (
    KIND_EVIDENCE,
    KIND_RESOLUTION,
    STATUS_CLAIMS,
    SYNC_HELD,
    SYNC_LOCAL_ONLY,
    SYNC_QUEUED,
    SYNC_SKIPPED,
    SYNC_SYNCED,
)

from . import conflicts as conflict_rules
from .conflicts import Claim
from .faces import blur_faces, detect_faces
from .memory import DeviceMemory, Hit, SearchSpec
from .merge import EDITABLE_FIELDS
from .policy import ItemFacts, PolicySettings, decide
from .store import DeviceDB

log = logging.getLogger("saakshi.field.service")

ACTION_TO_STATE = {
    "sync": SYNC_QUEUED,
    "sync_blurred": SYNC_QUEUED,
    "hold": SYNC_HELD,
    "local_only": SYNC_LOCAL_ONLY,
    "skip_duplicate": SYNC_SKIPPED,
}


@dataclass
class FieldSettings:
    device_id: str
    device_name: str
    data_root: Path
    qdrant_url: Optional[str] = None
    qdrant_api_key: Optional[str] = None
    collection: str = "saakshi_evidence"
    impact_url: Optional[str] = None
    ingest_token: Optional[str] = None
    sync_interval_s: float = 4.0
    pull_interval_s: float = 20.0
    conflict_window_s: float = 6 * 3600
    policy: PolicySettings = field(default_factory=PolicySettings)
    quantized: bool = False                 # keep int8 copies of vectors in RAM (4x smaller)
    ollama_url: Optional[str] = None        # optional local LLM for the assistant, e.g. http://127.0.0.1:11434
    ollama_model: Optional[str] = None      # e.g. qwen2.5:1.5b

    @property
    def device_dir(self) -> Path:
        return self.data_root / self.device_id


class FieldService:
    def __init__(self, settings: FieldSettings, embedder: Optional[Embedder] = None):
        self.s = settings
        d = settings.device_dir
        for sub in ("media", "thumbs", "upload"):
            (d / sub).mkdir(parents=True, exist_ok=True)
        self.db = DeviceDB(d / "device.sqlite3")
        self.memory = DeviceMemory(d / "memory", quantized=settings.quantized)
        self.embedder = embedder or Embedder()
        stored = self.db.get("policy")
        if isinstance(stored, dict):
            for k, v in stored.items():
                if hasattr(self.s.policy, k):
                    setattr(self.s.policy, k, v)

    # --------------------------------------------------------------- projects
    def projects(self) -> List[Project]:
        cached = self.db.get("projects_cache")
        data = cached if isinstance(cached, dict) and cached.get("projects") else load_bundled()
        return parse_projects(data)

    def project(self, project_id: Optional[str]) -> Optional[Project]:
        for p in self.projects():
            if p.id == project_id:
                return p
        return None

    def known_sites(self) -> List[Site]:
        """Sites auto-created on this device or learned from the mirror."""
        raw = self.db.get("auto_sites", []) or []
        return [Site(**s) for s in raw]

    def _remember_site(self, site: Site) -> None:
        if not site.auto:
            return
        raw = self.db.get("auto_sites", []) or []
        if not any(s["id"] == site.id for s in raw):
            raw.append({"id": site.id, "name": site.name, "lat": site.lat, "lon": site.lon, "project_id": site.project_id, "auto": True})
            self.db.set("auto_sites", raw)

    # ----------------------------------------------------------------- paths
    def media_path(self, evidence_id: str, ext: str) -> Path:
        return self.s.device_dir / "media" / f"{evidence_id}{ext}"

    def thumb_path(self, evidence_id: str) -> Path:
        return self.s.device_dir / "thumbs" / f"{evidence_id}.jpg"

    def upload_copy_path(self, evidence_id: str) -> Path:
        return self.s.device_dir / "upload" / f"{evidence_id}.jpg"

    # ----------------------------------------------------------------- ingest
    def ingest(
        self,
        data: bytes,
        filename: str,
        note: str = "",
        project_id: Optional[str] = None,
        status_claim: Optional[str] = None,
        consent: bool = False,
        private: bool = False,
    ) -> Dict[str, Any]:
        t0 = time.perf_counter()
        evidence_id = str(uuid.uuid4())
        ext = Path(filename).suffix.lower() or ".jpg"
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".heic", ".tif", ".tiff"):
            ext = ".jpg"
        img = open_image(data).convert("RGB")
        exif = read_exif(data)
        sha = sha256_bytes(data)
        dh = dhash(img)

        # Exact re-import of the same file on this device: return the existing item
        for pid, payload, _ in self.memory.find("local", sha256=sha, kind=KIND_EVIDENCE):
            self.db.log("ingest", f"Skipped {filename}: this exact file is already in memory", "warn", {"evidence_id": pid})
            return {"evidence_id": pid, "duplicate_file": True, **payload}

        self.media_path(evidence_id, ext).write_bytes(data)
        self.thumb_path(evidence_id).write_bytes(thumbnail_bytes(img))

        # Where and when
        projects = self.projects()
        project = self.project(project_id) if project_id else None
        if project is None:
            project = guess_project(projects, exif.lat, exif.lon) or (projects[0] if projects else None)
        site = assign_site(project, exif.lat, exif.lon, self.known_sites()) if project else None
        if site:
            self._remember_site(site)
            self.remember_site_name(site.id, site.name)
        captured_at = exif.captured_at or datetime.now().replace(microsecond=0).isoformat()
        captured_ts = iso_to_ts(captured_at) or time.time()

        # What: faces, embedding, offline tags
        faces = detect_faces(img)
        vector = self.embedder.embed_image(img)
        tags = [t for t, _ in self.embedder.zero_shot_tags(vector)]

        # Is this a repeat of something already known?
        neighbours = self.memory.nearest_images(vector, limit=3)
        best: Optional[Hit] = neighbours[0] if neighbours else None
        dup_sim = best.score if best else 0.0
        # With the colour-histogram fallback, confirm visual duplicates with the dHash too
        if best and not self.embedder.is_clip and hamming_hex(dh, best.payload.get("dhash")) > 10:
            dup_sim = min(dup_sim, 0.9)
        dup_synced = bool(best and (best.source == "mirror" or best.payload.get("sync_state") == SYNC_SYNCED))

        facts = ItemFacts(
            faces=len(faces),
            consent=consent,
            private=private,
            duplicate_of=best.id if best else None,
            duplicate_similarity=dup_sim,
            duplicate_synced=dup_synced,
            duplicate_gap_s=abs(captured_ts - float(best.payload.get("captured_ts") or 0)) if best else 0.0,
            new_info=bool((note or "").strip() or (status_claim in STATUS_CLAIMS)),
            novelty=1.0 - max(0.0, dup_sim),
            note=note,
            status_claim=status_claim if status_claim in STATUS_CLAIMS else None,
            has_gps=exif.has_gps,
            size_bytes=len(data),
        )
        decision = decide(facts, self.s.policy)

        if decision.action == "sync_blurred":
            self.upload_copy_path(evidence_id).write_bytes(compressed_bytes(blur_faces(img, faces), max_side=2048, quality=85))

        now = time.time()
        payload: Dict[str, Any] = {
            "kind": KIND_EVIDENCE,
            "evidence_id": evidence_id,
            "project_id": project.id if project else None,
            "project_name": project.name if project else None,
            "site_id": site.id if site else None,
            "site_name": site.name if site else None,
            "device_id": self.s.device_id,
            "device_name": self.s.device_name,
            "origin": "field",
            "file_name": filename,
            "file_ext": ext,
            "bytes": len(data),
            "width": img.width,
            "height": img.height,
            "sha256": sha,
            "dhash": dh,
            "captured_at": captured_at,
            "captured_ts": captured_ts,
            "has_exif": exif.has_exif,
            "has_gps": exif.has_gps,
            "lat": exif.lat,
            "lon": exif.lon,
            "camera": " ".join(x for x in (exif.make, exif.model) if x) or None,
            "edited_with": exif.edited_with,
            "note": note,
            "status_claim": facts.status_claim,
            "consent": consent,
            "private": private,
            "faces": len(faces),
            "tags": tags,
            "tag_source": "clip-on-device" if tags else None,
            "created_ts": now,
            "updated_ts": now,
            "version": 1,
            "field_ts": dict.fromkeys(EDITABLE_FIELDS, now),
            "field_by": dict.fromkeys(EDITABLE_FIELDS, self.s.device_id),
            "sync_state": ACTION_TO_STATE[decision.action],
            "sync_action": decision.action,
            "sync_reasons": decision.reasons,
            "priority": decision.priority,
            "tier": decision.tier,
            "duplicate_of": best.id if best and decision.action == "skip_duplicate" else None,
            "duplicate_similarity": round(dup_sim, 4),
        }
        if exif.has_gps:
            payload["loc"] = {"lat": exif.lat, "lon": exif.lon}

        self.memory.upsert(evidence_id, vector, bm25_document(search_text(payload)), payload)
        if decision.leaves_device:
            self.db.enqueue(evidence_id, decision.action, decision.tier, decision.priority, decision.reasons)

        claim_outcome = None
        if facts.status_claim and site:
            claim_outcome = self._apply_claim(
                Claim(site.id, facts.status_claim, captured_ts, self.s.device_id, evidence_id), project.id if project else None, "local"
            )

        ms = (time.perf_counter() - t0) * 1000
        self.db.log(
            "ingest",
            f"Imported {filename} → {decision.action.replace('_', ' ')} (priority {decision.priority})",
            "info",
            {"evidence_id": evidence_id, "reasons": decision.reasons, "ms": round(ms), "site": payload["site_name"]},
        )
        return {**payload, "ingest_ms": round(ms), "claim": claim_outcome}

    # ---------------------------------------------------------------- search
    def search(
        self,
        text: str,
        project_id: Optional[str] = None,
        limit: int = 24,
        recency_hours: Optional[float] = None,
        near_site: Optional[str] = None,
        lat: Optional[float] = None,
        lon: Optional[float] = None,
        radius_km: Optional[float] = None,
        boost_near: bool = False,
        diverse: bool = False,
        verified_only: bool = False,
        status: Optional[str] = None,
        source: str = "all",
    ) -> Dict[str, Any]:
        text = (text or "").strip()
        t0 = time.perf_counter()
        sparse = bm25_query(text) if text else None
        dense = self.embedder.embed_text(text) if (text and self.embedder.text_matches_images) else None
        embed_ms = (time.perf_counter() - t0) * 1000
        near = None
        if near_site:
            site = self.site_coords(near_site)
            if site:
                near = site
        elif lat is not None and lon is not None:
            near = (float(lat), float(lon))
        spec = SearchSpec(
            sparse=sparse, dense=dense, limit=limit, project_id=project_id or None,
            recency_hours=recency_hours or None, near=near,
            radius_m=(float(radius_km or 2.0) * 1000.0) if near else None,
            boost_near=bool(near and boost_near), diverse=diverse, verified_only=verified_only,
            status=status or None, source=source if source in ("all", "local", "mirror") else "all",
        )
        if not text and (near or verified_only or status):
            # Browsing without words: rank everything that passes the filters by recency
            spec.sparse = None
            spec.dense = None
            items = [i for i in self.list_evidence() if (not spec.verified_only or (i.get("cloud") or {}).get("integrity_level") == "verified")
                     and (not spec.status or i.get("status_claim") == spec.status)]
            if near:
                from saakshi_core.geo import haversine_m

                r = spec.radius_m or 2000.0
                items = [i for i in items if i.get("lat") is not None and haversine_m(near[0], near[1], i["lat"], i["lon"]) <= r]
            return {"query": text, "latency_ms": round((time.perf_counter() - t0) * 1000, 2), "embed_ms": round(embed_ms, 2),
                    "mode": "filters only", "plan": ["No words given: listing items that pass the filters, newest first"], "results": items[:limit]}
        res = self.memory.search(spec)
        words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2]
        results = []
        for h in res.hits:
            item = present(h.payload, h.id, h.source, h.score)
            hay = search_text(h.payload).lower()
            item["why"] = {"words": [w for w in words if w in hay], "semantic": dense is not None, "region": h.region or None}
            results.append(item)
        return {
            "query": text,
            "latency_ms": round(res.ms, 2),
            "embed_ms": round(embed_ms, 2),
            "mode": "hybrid (CLIP + BM25)" if dense is not None else "keyword (BM25)",
            "plan": res.plan,
            "shards": res.shards,
            "results": results,
        }

    def site_coords(self, site_id: str) -> Optional[Tuple[float, float]]:
        for p in self.projects():
            for st in p.sites:
                if st.id == site_id:
                    return (st.lat, st.lon)
        for st in self.known_sites():
            if st.id == site_id:
                return (st.lat, st.lon)
        for _pid, payload, _ in self.memory.scroll_all("local") + self.memory.scroll_all("mirror"):
            if payload.get("site_id") == site_id and payload.get("lat") is not None:
                return (payload["lat"], payload["lon"])
        return None

    def more_like_this(self, evidence_id: str, not_ids: Optional[List[str]] = None, limit: int = 8) -> List[Dict[str, Any]]:
        """Qdrant recommend query: like this photo, unlike the ones the officer rejected."""
        from saakshi_core.schema import VECTOR_CLIP

        def vec(pid: str) -> Optional[List[float]]:
            got = self.memory.get(pid, with_vector=True)
            if got and isinstance(got[1], dict) and got[1].get(VECTOR_CLIP) is not None:
                return list(got[1][VECTOR_CLIP])
            return None

        pos = vec(evidence_id)
        if pos is None:
            return []
        neg = [v for v in (vec(n) for n in (not_ids or [])) if v is not None]
        exclude = {evidence_id, *(not_ids or [])}
        hits = self.memory.recommend([pos], neg, limit=limit + len(exclude))
        return [present(h.payload, h.id, h.source, h.score) for h in hits if h.id not in exclude][:limit]

    def similar(self, evidence_id: str, limit: int = 8) -> List[Dict[str, Any]]:
        found = self.memory.get(evidence_id, with_vector=True)
        if not found:
            return []
        payload, vectors, _ = found
        from saakshi_core.schema import VECTOR_CLIP

        vec = (vectors or {}).get(VECTOR_CLIP) if isinstance(vectors, dict) else None
        if vec is None:
            return []
        return [present(h.payload, h.id, h.source, h.score) for h in self.memory.nearest_images(list(vec), limit=limit, exclude_id=evidence_id)]

    # ------------------------------------------------------------------ list
    def list_evidence(self, include_mirror: bool = True, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        items: Dict[str, Dict[str, Any]] = {}
        for pid, payload, _ in self.memory.scroll_all("local"):
            if payload.get("kind") == KIND_EVIDENCE:
                items[pid] = present(payload, pid, "local")
        if include_mirror:
            for pid, payload, _ in self.memory.scroll_all("mirror"):
                if payload.get("kind") == KIND_EVIDENCE and pid not in items:
                    items[pid] = present(payload, pid, "mirror")
        out = [i for i in items.values() if not project_id or i.get("project_id") == project_id]
        out.sort(key=lambda x: x.get("captured_ts") or 0, reverse=True)
        return out

    def get(self, evidence_id: str) -> Optional[Dict[str, Any]]:
        found = self.memory.get(evidence_id)
        if not found:
            return None
        payload, _, source = found
        return present(payload, evidence_id, source)

    # ------------------------------------------------------------------ edit
    def edit(self, evidence_id: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        found = self.memory.get(evidence_id)
        if not found:
            raise KeyError(evidence_id)
        payload, _, source = found
        if source != "local":
            raise PermissionError("Only evidence captured on this device can be edited here")
        now = time.time()
        field_ts = dict(payload.get("field_ts") or {})
        field_by = dict(payload.get("field_by") or {})
        update: Dict[str, Any] = {}
        for f in EDITABLE_FIELDS:
            if f in changes and changes[f] != payload.get(f):
                if f == "status_claim" and changes[f] not in STATUS_CLAIMS + [None, ""]:
                    continue
                update[f] = changes[f] or (None if f == "status_claim" else changes[f])
                field_ts[f] = now
                field_by[f] = self.s.device_id
        if not update:
            return present(payload, evidence_id, source)
        update.update({"field_ts": field_ts, "field_by": field_by, "updated_ts": now, "version": int(payload.get("version", 1)) + 1})
        merged = {**payload, **update}

        # Re-evaluate the sync decision when privacy-relevant fields change
        if "private" in update or "consent" in update:
            facts = ItemFacts(
                faces=int(merged.get("faces", 0)),
                consent=bool(merged.get("consent")),
                private=bool(merged.get("private")),
                note=merged.get("note") or "",
                status_claim=merged.get("status_claim"),
                has_gps=bool(merged.get("has_gps")),
            )
            decision = decide(facts, self.s.policy)
            update.update({"sync_action": decision.action, "sync_reasons": decision.reasons, "priority": decision.priority})
            if decision.leaves_device:
                if decision.action == "sync_blurred" and not self.upload_copy_path(evidence_id).exists():
                    img = open_image(self.media_path(evidence_id, merged.get("file_ext", ".jpg")))
                    self.upload_copy_path(evidence_id).write_bytes(compressed_bytes(blur_faces(img, detect_faces(img)), max_side=2048, quality=85))
                update["sync_state"] = SYNC_QUEUED
                self.db.enqueue(evidence_id, decision.action, decision.tier, decision.priority, decision.reasons,
                                op="update" if payload.get("sync_state") == SYNC_SYNCED else "push")
            else:
                update["sync_state"] = ACTION_TO_STATE[decision.action]
        elif payload.get("sync_state") == SYNC_SYNCED:
            # Already on the server: send the edit as an update
            update["sync_state"] = SYNC_QUEUED
            self.db.enqueue(evidence_id, payload.get("sync_action", "sync"), "compressed", max(60, int(payload.get("priority", 50))),
                            [f"Edited on device: {', '.join(k for k in update if k in EDITABLE_FIELDS)}"], op="update")
        elif payload.get("sync_state") == SYNC_QUEUED:
            pass  # the queued push will carry the new values

        self.memory.set_payload(evidence_id, update)
        merged = {**payload, **update}
        # Re-index text so search reflects the edit
        found_v = self.memory.get(evidence_id, with_vector=True)
        if found_v and isinstance(found_v[1], dict):
            from saakshi_core.schema import VECTOR_CLIP

            self.memory.upsert(evidence_id, list(found_v[1][VECTOR_CLIP]), bm25_document(search_text(merged)), merged)
        if "status_claim" in update and merged.get("status_claim") and merged.get("site_id"):
            self._apply_claim(Claim(merged["site_id"], merged["status_claim"], now, self.s.device_id, evidence_id), merged.get("project_id"), "local")
        self.db.log("edit", f"Edited {merged.get('file_name')}: {', '.join(k for k in update if k in EDITABLE_FIELDS)}", "info", {"evidence_id": evidence_id})
        return present(merged, evidence_id, "local")

    def approve_held(self, evidence_id: str, mode: str = "blur") -> Dict[str, Any]:
        """Officer releases a held item: 'blur' uploads a blurred copy, 'consent' records consent."""
        if mode == "consent":
            return self.edit(evidence_id, {"consent": True})
        found = self.memory.get(evidence_id)
        if not found:
            raise KeyError(evidence_id)
        payload, _, _ = found
        img = open_image(self.media_path(evidence_id, payload.get("file_ext", ".jpg")))
        self.upload_copy_path(evidence_id).write_bytes(compressed_bytes(blur_faces(img, detect_faces(img)), max_side=2048, quality=85))
        reasons = [f"{payload.get('faces', 0)} faces blurred on the device, released by the officer"]
        self.memory.set_payload(evidence_id, {"sync_state": SYNC_QUEUED, "sync_action": "sync_blurred", "sync_reasons": reasons, "updated_ts": time.time()})
        self.db.enqueue(evidence_id, "sync_blurred", "full", max(50, int(payload.get("priority", 50))), reasons)
        self.db.log("policy", f"Released {payload.get('file_name')} with faces blurred", "info", {"evidence_id": evidence_id})
        return self.get(evidence_id) or {}

    # ------------------------------------------------------------- conflicts
    def _apply_claim(self, claim: Claim, project_id: Optional[str], source: str) -> Dict[str, str]:
        current = self.db.site_claim(claim.site_id)
        outcome = conflict_rules.evaluate(current, claim, self.s.conflict_window_s)
        if outcome.kind in ("new", "newer_wins") or (outcome.kind == "same" and current and claim.ts > current.ts):
            self.db.set_site_claim(claim, project_id, source)
            closed = self.db.supersede_conflicts(claim.site_id, claim.ts)
            if closed:
                self.db.log("conflict", f"{closed} conflict(s) at {claim.site_id} closed: newer evidence ({claim.status}) arrived", "info")
        if outcome.kind == "conflict" and current is not None:
            local, remote = (current, claim) if source == "remote" else (claim, current)
            if self.db.add_conflict(claim.site_id, project_id, local, remote, outcome.message):
                self.db.log("conflict", f"Conflict at {claim.site_id}: {outcome.message}", "warn", {"site_id": claim.site_id})
        elif outcome.kind == "newer_wins" and source == "remote":
            self.db.log("merge", f"Site {claim.site_id}: {outcome.message} (from {claim.device_id})", "info", {"site_id": claim.site_id})
        return {"kind": outcome.kind, "message": outcome.message}

    def resolve_conflict(self, conflict_id: int, choice: str, status: Optional[str] = None) -> Dict[str, Any]:
        c = self.db.conflict(conflict_id)
        if not c:
            raise KeyError(conflict_id)
        if choice == "local":
            final = c["local_status"]
        elif choice == "remote":
            final = c["remote_status"]
        else:
            final = status if status in STATUS_CLAIMS else c["local_status"]
        now = time.time()
        rid = str(uuid.uuid4())
        text = f"resolution site {c['site_id']} status {final}"
        payload = {
            "kind": KIND_RESOLUTION,
            "evidence_id": rid,
            "project_id": c["project_id"],
            "site_id": c["site_id"],
            "status_claim": final,
            "resolves": [c["local_evidence"], c["remote_evidence"]],
            "device_id": self.s.device_id,
            "device_name": self.s.device_name,
            "origin": "field",
            "note": f"Resolved: {c['message']} → {final}",
            "captured_ts": now,
            "created_ts": now,
            "updated_ts": now,
            "version": 1,
            "sync_state": SYNC_QUEUED,
        }
        self.memory.upsert(rid, self.embedder.embed_text(text), bm25_document(text), payload)
        self.db.enqueue(rid, "sync", "compressed", 90, ["Conflict resolution: sent first so other devices converge"], op="resolution")
        self.db.set_site_claim(Claim(c["site_id"], final, now, self.s.device_id, rid), c["project_id"], "resolution")
        self.db.resolve_conflict(conflict_id, f"{choice}:{final}")
        self.db.log("conflict", f"Resolved conflict at {c['site_id']}: {final}", "info", {"conflict_id": conflict_id})
        return {"resolution_id": rid, "status": final}

    def apply_remote(self, payload: Dict[str, Any]) -> None:
        """Process a point that arrived from the server mirror."""
        kind = payload.get("kind")
        if kind == KIND_RESOLUTION:
            resolves = set(payload.get("resolves") or [])
            for c in self.db.conflicts("open"):
                if {c["local_evidence"], c["remote_evidence"]} == resolves:
                    self.db.resolve_conflict(c["id"], f"remote:{payload.get('status_claim')}")
                    self.db.log("conflict", f"Conflict at {c['site_id']} resolved on {payload.get('device_id')}: {payload.get('status_claim')}")
            if payload.get("site_id") and payload.get("status_claim"):
                self.db.set_site_claim(
                    Claim(payload["site_id"], payload["status_claim"], float(payload.get("captured_ts") or time.time()),
                          payload.get("device_id", "?"), payload.get("evidence_id", "")),
                    payload.get("project_id"), "resolution",
                )
            return
        if kind == KIND_EVIDENCE:
            if payload.get("site_id") and payload.get("site_name"):
                self.remember_site_name(payload["site_id"], payload["site_name"])
            if payload.get("site_id") and payload.get("status_claim"):
                self._apply_claim(
                    Claim(payload["site_id"], payload["status_claim"], float(payload.get("captured_ts") or 0),
                          payload.get("device_id", "?"), payload.get("evidence_id", "")),
                    payload.get("project_id"), "remote",
                )
            if payload.get("site_id") and payload.get("lat") is not None and payload.get("site_name", "").startswith("Spot "):
                self._remember_site(Site(id=payload["site_id"], name=payload["site_name"], lat=payload["lat"], lon=payload["lon"],
                                         project_id=payload.get("project_id") or "", auto=True))

    def remember_site_name(self, site_id: str, name: str) -> None:
        names = self.db.get("site_names", {}) or {}
        if names.get(site_id) != name:
            names[site_id] = name
            self.db.set("site_names", names)

    def site_name(self, site_id: Optional[str]) -> str:
        if not site_id:
            return ""
        names = self.db.get("site_names", {}) or {}
        if site_id in names:
            return names[site_id]
        for p in self.projects():
            for s in p.sites:
                if s.id == site_id:
                    return s.name
        return site_id

    # ---------------------------------------------------------- maintenance
    def maintenance(self, min_new_points: int = 200) -> Optional[Dict[str, Any]]:
        """Background housekeeping: index new vectors once enough have arrived."""
        pts = self.memory.stats()["local_points"]
        last = int(self.db.get("optimized_at_points", 0) or 0)
        if pts - last < min_new_points:
            return None
        res = self.memory.optimize()
        self.db.set("optimized_at_points", pts)
        self.db.set("last_optimize", {**res, "ts": time.time()})
        self.db.log("memory", f"Indexed memory: {res['indexed_vectors']} vectors in HNSW, {res['segments']} segment(s), {res['seconds']} s", "info")
        return res

    # -------------------------------------------------------------- regions
    def subscribed_regions(self) -> List[str]:
        """Projects whose cloud knowledge this device keeps a mirror of."""
        from saakshi_core.qdrant_server import UNASSIGNED

        chosen = self.db.get("regions")
        if isinstance(chosen, list):
            return chosen
        return [p.id for p in self.projects()] + [UNASSIGNED]

    def set_regions(self, regions: List[str]) -> List[str]:
        known = {p.id for p in self.projects()} | {"unassigned"}
        clean = [r for r in regions if r in known]
        self.db.set("regions", clean)
        self.db.log("sync", f"This device now mirrors {len(clean)} region(s): {', '.join(clean) or 'none'}", "info")
        return clean

    # -------------------------------------------------------------- storage
    def _dir_bytes(self, sub: str) -> int:
        d = self.s.device_dir / sub
        return sum(f.stat().st_size for f in d.glob("*") if f.is_file()) if d.exists() else 0

    def storage(self) -> Dict[str, Any]:
        mem = self.memory.stats()
        evicted = sum(1 for _pid, p, _ in self.memory.scroll_all("local") if p.get("media_evicted"))
        return {
            "originals_bytes": self._dir_bytes("media"),
            "thumbs_bytes": self._dir_bytes("thumbs"),
            "upload_copies_bytes": self._dir_bytes("upload"),
            "local_shard_bytes": mem["local_bytes"],
            "mirror_bytes": sum(mem["mirror_bytes"].values()),
            "budget_bytes": int(self.s.policy.media_budget_mb * 1024 * 1024),
            "originals_released": evicted,
        }

    def enforce_storage_budget(self) -> int:
        """Release originals that are safely in the cloud when the device is over its budget.

        Only photos that are synced AND checked by HQ are eligible, oldest first.
        The vectors, payload and thumbnail stay, so the item is still searchable offline.
        Private and held items are never touched.
        """
        budget = int(self.s.policy.media_budget_mb * 1024 * 1024)
        used = self._dir_bytes("media")
        if used <= budget:
            return 0
        target = int(budget * 0.9)
        eligible = []
        for pid, payload, _ in self.memory.scroll_all("local"):
            cloud = payload.get("cloud") or {}
            if payload.get("kind") != KIND_EVIDENCE or payload.get("media_evicted") or payload.get("private"):
                continue
            if payload.get("sync_state") != SYNC_SYNCED or not cloud.get("public_id") or not cloud.get("integrity_level"):
                continue
            path = self.media_path(pid, payload.get("file_ext", ".jpg"))
            if path.exists():
                eligible.append((float(payload.get("captured_ts") or 0), pid, path, payload))
        eligible.sort()
        released = 0
        for _ts, pid, path, _payload in eligible:
            if used <= target:
                break
            size = path.stat().st_size
            path.unlink(missing_ok=True)
            self.upload_copy_path(pid).unlink(missing_ok=True)
            self.memory.set_payload(pid, {"media_evicted": True})
            used -= size
            released += 1
        if released:
            self.db.log("storage", f"Released {released} original(s) already safe in the cloud to stay under the "
                                   f"{self.s.policy.media_budget_mb:g} MB budget; thumbnails and search memory kept", "info")
        return released

    # ------------------------------------------------------------- timeline
    def site_timeline(self, site_id: str) -> Dict[str, Any]:
        """How knowledge about one place evolved: photos, status reports, HQ verdicts, conflicts, decisions."""
        events: List[Dict[str, Any]] = []
        seen = set()
        for source in ("local", "mirror"):
            for pid, p, _ in self.memory.scroll_all(source):
                if p.get("site_id") != site_id or pid in seen:
                    continue
                seen.add(pid)
                who = p.get("device_name") or p.get("device_id")
                if p.get("kind") == KIND_RESOLUTION:
                    events.append({"ts": p.get("captured_ts"), "type": "decision", "who": who, "status": p.get("status_claim"),
                                   "text": p.get("note") or "Conflict resolved", "id": pid})
                    continue
                cloud = p.get("cloud") or {}
                events.append({"ts": p.get("captured_ts"), "type": "photo", "who": who, "device_id": p.get("device_id"), "status": p.get("status_claim"),
                               "text": p.get("note") or p.get("file_name") or "Photo", "id": pid, "source": source,
                               "hq": cloud.get("integrity_level"), "hq_score": cloud.get("integrity_score"),
                               "tags": cloud.get("ai_tags") or p.get("tags") or []})
        for c in self.db.conflicts():
            if c["site_id"] != site_id:
                continue
            events.append({"ts": c["created_ts"], "type": "conflict", "who": f"{c['local_device']} vs {c['remote_device']}",
                           "text": c["message"], "state": c["state"], "resolution": c.get("resolution")})
        events.sort(key=lambda e: float(e.get("ts") or 0))
        current = self.db.site_claim(site_id)
        return {"site_id": site_id, "site_name": self.site_name(site_id),
                "current": {"status": current.status, "ts": current.ts, "device": current.device_id} if current else None,
                "events": events}

    def sites_overview(self) -> List[Dict[str, Any]]:
        states = {s["site_id"]: s for s in self.db.site_states()}
        counts: Dict[str, int] = {}
        for _pid, p, _ in self.memory.scroll_all("local") + self.memory.scroll_all("mirror"):
            if p.get("kind") == KIND_EVIDENCE and p.get("site_id"):
                counts[p["site_id"]] = counts.get(p["site_id"], 0) + 1
        ids = set(states) | set(counts)
        out = []
        for sid in ids:
            st = states.get(sid) or {}
            out.append({"site_id": sid, "site_name": self.site_name(sid), "status": st.get("status"), "ts": st.get("ts"),
                        "device_id": st.get("device_id"), "source": st.get("source"), "evidence": counts.get(sid, 0),
                        "open_conflicts": sum(1 for c in self.db.conflicts("open") if c["site_id"] == sid)})
        out.sort(key=lambda x: -(x.get("ts") or 0))
        return out

    # ------------------------------------------------------------------ stats
    def stats(self) -> Dict[str, Any]:
        mem = self.memory.stats()
        mem["colleague_points"] = self.memory.colleague_points(self.s.device_id)
        by_state = dict(self.memory.facet("sync_state"))
        return {
            "device_id": self.s.device_id,
            "device_name": self.s.device_name,
            "embedder": {"mode": self.embedder.load(), "label": self.embedder.label, "error": self.embedder.error},
            "memory": mem,
            "sync_states": by_state,
            "outbox": self.db.outbox_counts(),
            "open_conflicts": len(self.db.conflicts("open")),
            "sites": self.db.site_states(),
            "storage": self.storage(),
            "regions": {"subscribed": self.subscribed_regions(), "available": [{"id": p.id, "name": p.name} for p in self.projects()]},
            "assistant": {"llm": self.s.ollama_model if self.s.ollama_url and self.s.ollama_model else None},
            "last_optimize": self.db.get("last_optimize"),
        }

    def save_policy(self, changes: Dict[str, Any]) -> PolicySettings:
        p = self.s.policy
        if "duplicate_threshold" in changes:
            p.duplicate_threshold = max(0.5, min(0.999, float(changes["duplicate_threshold"])))
        if changes.get("privacy_mode") in ("blur", "hold"):
            p.privacy_mode = changes["privacy_mode"]
        if "metered" in changes:
            p.metered = bool(changes["metered"])
        if "auto_bandwidth" in changes:
            p.auto_bandwidth = bool(changes["auto_bandwidth"])
        if "media_budget_mb" in changes:
            p.media_budget_mb = max(0.0, float(changes["media_budget_mb"]))
        self.db.set("policy", {"duplicate_threshold": p.duplicate_threshold, "privacy_mode": p.privacy_mode, "metered": p.metered,
                               "auto_bandwidth": p.auto_bandwidth, "media_budget_mb": p.media_budget_mb})
        if "media_budget_mb" in changes:
            self.enforce_storage_budget()
        return p


def search_text(payload: Dict[str, Any]) -> str:
    """Text indexed by BM25 for one evidence item."""
    parts = [
        payload.get("note") or "",
        " ".join(taxonomy.label(t) for t in payload.get("tags") or []),
        payload.get("site_name") or "",
        payload.get("project_name") or "",
        (payload.get("status_claim") or "").replace("_", " "),
        payload.get("file_name") or "",
    ]
    cloud = payload.get("cloud") or {}
    parts.append(cloud.get("caption") or "")
    parts.append(" ".join(taxonomy.label(t) for t in cloud.get("ai_tags") or []))
    return " ".join(p for p in parts if p)


def present(payload: Dict[str, Any], pid: str, source: str, score: Optional[float] = None) -> Dict[str, Any]:
    """Shape a payload for the UI."""
    out = {k: v for k, v in payload.items() if k not in ("field_ts", "field_by")}
    out["id"] = pid
    out["source"] = source
    if score is not None:
        out["score"] = round(score, 4)
    return out
