"""The cloud side of Saakshi: turn raw field photos into verified, searchable evidence.

Ingest pipeline (same for web uploads and field-device sync):
  1. read EXIF (or trust the device's reading of the original)
  2. place it: project by GPS, site by 150 m clustering
  3. upload to Cloudinary with media_metadata, faces, phash, colors, quality_analysis
  4. understand it: AI Vision tagging + caption (CLIP zero-shot as fallback)
  5. check it: reuse against every earlier submission, integrity score
  6. write results back to Cloudinary (context + tags) and to the shared
     Qdrant point, so the device that captured it sees the verdict
  7. re-pair before/after for the site
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from saakshi_core import taxonomy
from saakshi_core.embeddings import Embedder, bm25_document, bm25_query, cosine
from saakshi_core.imaging import ExifInfo, dhash, fit_within, hamming_hex, iso_to_ts, open_image, read_exif, sha256_bytes
from saakshi_core.projects import Project, Site, assign_site, guess_project, load_bundled
from saakshi_core.schema import KIND_EVIDENCE, STATUS_CLAIMS

from . import pairing, transforms
from .cloud import CAPTION_PROMPT, CHANGE_PROMPT, CloudError, CloudinaryStore, LocalStore
from .db import ImpactDB
from .integrity import IntegrityInput, ReuseMatch, score
from .vectors import LocalIndex, QdrantIndex

log = logging.getLogger("saakshi.impact")


@dataclass
class ImpactSettings:
    data_dir: Path
    org_name: str = "Green Delta Collective (demo NGO)"
    cloudinary_url: Optional[str] = None
    cloud_name: Optional[str] = None
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    folder: str = "saakshi"
    ai_vision: bool = True
    qdrant_url: Optional[str] = None
    qdrant_api_key: Optional[str] = None
    collection: str = "saakshi_evidence"
    ingest_token: Optional[str] = None
    public_base: str = ""
    llm_base_url: Optional[str] = None
    llm_api_key: Optional[str] = None
    llm_model: Optional[str] = None
    pair_min_gap_hours: float = 1.0
    # Largest image Cloudinary accepts (10 MB on the free plan); bigger photos are stored as a smaller copy
    max_image_bytes: int = 10 * 1024 * 1024
    # Public read-only demo: visitors browse; the dashboard hides every control that writes
    public_demo: bool = False
    demo_video_url: str = ""
    repo_url: str = ""


class IngestInProgress(RuntimeError):
    """The same evidence is already being ingested (a device retried mid-request)."""


def _parse_cloudinary_url(url: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    m = re.match(r"cloudinary://([^:]+):([^@]+)@(.+)", url.strip())
    if not m:
        return None, None, None
    return m.group(3), m.group(1), m.group(2)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48] or "project"


class ImpactService:
    def __init__(self, s: ImpactSettings, embedder: Optional[Embedder] = None, store=None):
        self.s = s
        self.db = ImpactDB(Path(s.data_dir) / "impact.sqlite3")
        self.embedder = embedder or Embedder()
        if store is not None:
            self.store = store
        else:
            cloud, key, secret = s.cloud_name, s.api_key, s.api_secret
            if s.cloudinary_url:
                cloud, key, secret = _parse_cloudinary_url(s.cloudinary_url)
            if cloud and key and secret:
                self.store = CloudinaryStore(cloud, key, secret, folder=s.folder, ai_vision=s.ai_vision)
            else:
                log.warning("Cloudinary not configured: running with local file storage (no transformations, no AI Vision)")
                self.store = LocalStore(Path(s.data_dir) / "media", s.public_base)
        self.index = QdrantIndex(s.qdrant_url, s.qdrant_api_key, s.collection) if s.qdrant_url else LocalIndex(self.db)
        self._lock = threading.RLock()
        self._ingesting: set = set()  # evidence ids being processed right now
        if not self.db.projects():
            self._seed_projects()

    # ---------------------------------------------------------------- projects
    def _seed_projects(self) -> None:
        for p in load_bundled()["projects"]:
            self.db.upsert_project(p)
            for site in p.get("sites", []):
                self.db.upsert_site({**site, "project_id": p["id"], "auto": False})

    def projects(self) -> List[Project]:
        out = []
        for row in self.db.projects():
            sites = [Site(id=s["id"], name=s["name"], lat=s["lat"], lon=s["lon"], project_id=s["project_id"], auto=bool(s["auto"]))
                     for s in self.db.sites(row["id"])]
            out.append(Project(id=row["id"], name=row["name"], activity=row.get("activity") or "", description=row.get("description") or "",
                               lat=row["lat"], lon=row["lon"], radius_m=row["radius_m"], start_date=row["start_date"],
                               end_date=row["end_date"], sites=sites))
        return out

    def project(self, pid: Optional[str]) -> Optional[Project]:
        for p in self.projects():
            if p.id == pid:
                return p
        return None

    def create_project(self, body: Dict[str, Any]) -> Dict[str, Any]:
        pid = _slug(body.get("id") or body["name"])
        base = pid
        n = 2
        while self.db.project(pid):
            pid = f"{base}-{n}"
            n += 1
        self.db.upsert_project({**body, "id": pid})
        self.db.event("project", f"Project created: {body['name']}", pid)
        return self.db.project(pid) or {}

    # ------------------------------------------------------------------ ingest
    def ingest(self, data: bytes, filename: str, meta: Dict[str, Any], source: str) -> Dict[str, Any]:
        evidence_id = str(meta.get("evidence_id") or uuid.uuid4())
        existing = self.db.asset(evidence_id)
        if existing:
            return {"asset": self.present(existing), "cloud": self.cloud_summary(existing), "duplicate_request": True}
        # A device that lost the connection mid-upload retries while the first request may still
        # be running here; processing it twice would spend AI Vision quota and collide on insert.
        with self._lock:
            if evidence_id in self._ingesting:
                raise IngestInProgress(f"Evidence {evidence_id} is already being processed; retry shortly")
            self._ingesting.add(evidence_id)
        try:
            return self._ingest(evidence_id, data, filename, meta, source)
        finally:
            with self._lock:
                self._ingesting.discard(evidence_id)

    def _ingest(self, evidence_id: str, data: bytes, filename: str, meta: Dict[str, Any], source: str) -> Dict[str, Any]:

        is_video = _is_video(filename, data)
        img = None if is_video else open_image(data).convert("RGB")
        from_device = source == "field"
        exif = read_exif(data) if not is_video else ExifInfo()
        lat = meta.get("lat") if from_device else exif.lat
        lon = meta.get("lon") if from_device else exif.lon
        has_gps = lat is not None and lon is not None
        has_exif = bool(meta.get("has_exif")) if from_device else exif.has_exif
        captured_at = (meta.get("captured_at") if from_device else exif.captured_at) or None
        edited_with = meta.get("edited_with") if from_device else exif.edited_with
        camera = meta.get("camera") if from_device else (" ".join(x for x in (exif.make, exif.model) if x) or None)
        sha = meta.get("sha256") or sha256_bytes(data)
        dh = dhash(img) if img is not None else None
        uploaded_at = _now_iso()

        # Place it
        projects = self.projects()
        project = self.project(meta.get("project_id")) if meta.get("project_id") else None
        if project is None:
            project = guess_project(projects, lat, lon) or (projects[0] if projects else None)
        site = None
        if meta.get("site_id"):
            row = self.db.site(meta["site_id"])
            if row:
                site = Site(id=row["id"], name=row["name"], lat=row["lat"], lon=row["lon"], project_id=row["project_id"], auto=bool(row["auto"]))
        if site is None and project is not None:
            site = assign_site(project, lat, lon)
            if meta.get("site_id") and site.auto and meta.get("site_name"):
                site = Site(id=meta["site_id"], name=meta["site_name"], lat=site.lat, lon=site.lon, project_id=project.id, auto=True)
        if site is not None:
            self.db.upsert_site({"id": site.id, "project_id": site.project_id or (project.id if project else ""), "name": site.name,
                                 "lat": site.lat, "lon": site.lon, "auto": site.auto})

        # Upload to Cloudinary
        public_id = f"{self.s.folder}/{evidence_id}"
        asset_folder = f"{self.s.folder}/{project.id if project else 'unsorted'}/{site.id if site else 'unsorted'}"
        context = {
            "evidence_id": evidence_id,
            "project": project.id if project else "",
            "site": site.id if site else "",
            "site_name": site.name if site else "",
            "captured_at": captured_at or "",
            "lat": f"{lat:.6f}" if has_gps else "",
            "lon": f"{lon:.6f}" if has_gps else "",
            "device": meta.get("device_id") or "web",
            "source": source,
            "sha256": sha,
            "note": (meta.get("note") or "")[:500],
            "status": meta.get("status_claim") or "",
            "consent": "yes" if meta.get("consent") else "no",
        }
        tags = ["saakshi", f"source_{source}"] + ([f"project_{project.id}"] if project else [])
        stored = data
        if not is_video and len(data) > self.s.max_image_bytes:
            # Over the storage limit: keep a smaller copy. The hash, fingerprints and EXIF above are the original's.
            stored = fit_within(data, self.s.max_image_bytes)
            log.info("%s is %.1f MB, over the %.0f MB limit: storing a %.1f MB copy", filename, len(data) / 1e6,
                     self.s.max_image_bytes / 1e6, len(stored) / 1e6)
        res = self.store.upload(stored, public_id, asset_folder, tags, context, resource_type="video" if is_video else "image")
        public_id = res.get("public_id", public_id)
        secure_url = res.get("secure_url") or self.store.url_original(public_id)
        if is_video:
            # Videos carry their own metadata; phones write GPS and creation time into it
            vmeta = res.get("media_metadata") or res.get("image_metadata") or {}
            vlat, vlon, vtime = _video_meta(vmeta)
            if not has_gps and vlat is not None:
                lat, lon, has_gps = vlat, vlon, True
            if not captured_at and vtime:
                captured_at = vtime
            has_exif = has_exif or bool(vmeta)
            if has_gps and (site is None or site.id.endswith("-unlocated")):
                # GPS only became known after upload: place the video now
                if not meta.get("project_id"):
                    project = guess_project(projects, lat, lon) or project
                if project is not None:
                    site = assign_site(project, lat, lon)
                    self.db.upsert_site({"id": site.id, "project_id": project.id, "name": site.name, "lat": site.lat, "lon": site.lon, "auto": site.auto})
                    self.store.update(public_id, context={"project": project.id, "site": site.id, "site_name": site.name,
                                                          "lat": f"{lat:.6f}", "lon": f"{lon:.6f}"}, resource_type="video")
            img = self._fetch_frame(public_id)
            if img is not None:
                dh = dhash(img)
        faces_cloud = len(res.get("faces") or [])
        faces = max(int(meta.get("faces") or 0), faces_cloud)
        quality = None
        qa = res.get("quality_analysis")
        if isinstance(qa, dict) and qa.get("focus") is not None:
            quality = float(qa["focus"])
        phash = res.get("phash") or dh

        # Understand it
        vector = None
        if isinstance(self.index, QdrantIndex):
            got = self.index.get(evidence_id)
            if got and got[0]:
                vector = got[0]
        if vector is None and img is not None:
            vector = self.embedder.embed_image(img)
        analysis_url = transforms.video_frame(public_id, 1280, 960) if is_video else secure_url
        ai_tags, caption, vision_source = self._understand(analysis_url, public_id, vector, is_video)

        # Check it
        this_ts = iso_to_ts(captured_at) or time.time()
        reuse, later_copies = self._find_reuse(evidence_id, sha, phash, dh, vector, this_ts)
        integ = score(IntegrityInput(
            has_exif=has_exif, has_gps=has_gps, lat=lat, lon=lon, captured_at=captured_at, uploaded_at=uploaded_at,
            edited_with=edited_with, quality=quality, faces=faces, consent=bool(meta.get("consent")),
            project=project, site_id=site.id if site else None, reuse=reuse, is_video=is_video,
        ))

        asset = {
            "id": evidence_id,
            "project_id": project.id if project else None,
            "site_id": site.id if site else None,
            "public_id": public_id,
            "version": res.get("version"),
            "secure_url": secure_url,
            "format": res.get("format"),
            "width": res.get("width") or (img.width if img is not None else None),
            "height": res.get("height") or (img.height if img is not None else None),
            "media_type": "video" if is_video else "image",
            "duration": res.get("duration"),
            "bytes": res.get("bytes") or len(data),
            "sha256": sha,
            "phash": phash,
            "dhash": dh,
            "lat": lat,
            "lon": lon,
            "captured_at": captured_at,
            "captured_ts": iso_to_ts(captured_at) or time.time(),
            "uploaded_at": uploaded_at,
            "uploaded_ts": time.time(),
            "device_id": meta.get("device_id") or "web",
            "device_name": meta.get("device_name") or ("Web upload" if source == "web" else meta.get("device_id")),
            "source": source,
            "note": meta.get("note") or "",
            "status_claim": meta.get("status_claim") if meta.get("status_claim") in STATUS_CLAIMS else None,
            "consent": 1 if meta.get("consent") else 0,
            "faces": faces,
            "blurred_on_device": 1 if meta.get("blurred_on_device") else 0,
            "has_exif": 1 if has_exif else 0,
            "has_gps": 1 if has_gps else 0,
            "camera": camera,
            "edited_with": edited_with,
            "ai_tags": ai_tags,
            "device_tags": meta.get("tags") or [],
            "caption": caption,
            "vision_source": vision_source,
            "quality": quality,
            "colors": (res.get("colors") or [])[:6],
            "integrity_score": integ.score,
            "integrity_level": integ.level,
            "integrity": integ.to_dict(),
            "reuse_of": _reuse_of(reuse, integ),
            "raw": {**{k: res.get(k) for k in ("asset_id", "created_at", "etag", "asset_folder", "media_metadata", "image_metadata") if res.get(k)},
                    **({"original_bytes": len(data)} if stored is not data else {})},
        }
        with self._lock:
            self.db.insert_asset(asset, vector)

        # Write back: Cloudinary context/tags, shared Qdrant point
        self.store.update(public_id, context={
            "integrity": str(integ.score), "integrity_level": integ.level, "caption": (caption or "")[:900],
            "ai_tags": ";".join(ai_tags),
            # what a rebuild needs to show the photo as it was checked (faces decide blurring on public pages)
            "faces": str(faces), "has_exif": "1" if has_exif else "0", "edited_with": (edited_with or "")[:120],
            "quality": f"{quality:.3f}" if quality is not None else "", "phash": phash or "", "dhash": dh or "",
            "device_name": (asset["device_name"] or "")[:120],
        }, tags=[f"integrity_{integ.level}"] + [f"ai_{t}" for t in ai_tags], resource_type="video" if is_video else "image")
        self.store.update(public_id, context=_checks_context(integ, asset["reuse_of"]), resource_type="video" if is_video else "image")
        cloud = self.cloud_summary(self.db.asset(evidence_id) or asset)
        payload_text = " ".join([asset["note"], caption or "", " ".join(taxonomy.label(t) for t in ai_tags), site.name if site else "", project.name if project else ""])
        if isinstance(self.index, QdrantIndex) and vector is not None:
            got = self.index.get(evidence_id)
            if got:
                self.index.enrich(evidence_id, {"cloud": cloud}, asset["project_id"])
            else:
                self.index.upsert(evidence_id, vector, bm25_document(payload_text), {
                    "kind": KIND_EVIDENCE, "evidence_id": evidence_id, "origin": "impact", "device_id": asset["device_id"],
                    "device_name": asset["device_name"], "project_id": asset["project_id"], "project_name": project.name if project else None,
                    "site_id": asset["site_id"], "site_name": site.name if site else None, "note": asset["note"],
                    "status_claim": asset["status_claim"], "captured_at": captured_at, "captured_ts": asset["captured_ts"],
                    "lat": lat, "lon": lon, **({"loc": {"lat": lat, "lon": lon}} if has_gps else {}),
                    "file_name": filename, "tags": ai_tags, "cloud": cloud, "created_ts": time.time(), "updated_ts": time.time(),
                    "version": 1, "sync_state": "synced", "has_gps": has_gps, "faces": faces,
                })

        # Log and re-pair
        pname = project.name if project else "no project"
        self.db.event("ingest", f"{filename} from {asset['device_name']} → {site.name if site else 'no site'} ({integ.level}, {integ.score})",
                      asset["project_id"], evidence_id, "warn" if integ.level != "verified" else "info")
        for c in integ.checks:
            if c.status == "fail":
                self.db.event("integrity", f"{filename}: {c.message}", asset["project_id"], evidence_id, "error")
        if site is not None:
            self.refresh_pair(site.id)
        # This photo was taken before near-identical ones already on file: they are the copies
        for other_id in later_copies:
            self.rescore(other_id, reason=f"an earlier original ({filename}) arrived")
        log.info("Ingested %s into %s", filename, pname)
        return {"asset": self.present(self.db.asset(evidence_id) or asset), "cloud": cloud}

    def _understand(self, url: str, public_id: str, vector: Optional[List[float]], is_video: bool = False) -> Tuple[List[str], Optional[str], str]:
        tags: List[str] = []
        caption: Optional[str] = None
        source = "none"
        if getattr(self.store, "ai_vision_enabled", False):
            analysis_url = url if is_video or not self.store.transformations else transforms.analysis_jpg(self.store.url_display(public_id))
            errors = []
            # Tags and caption are separate requests, sent together; one failing must not lose the other
            with ThreadPoolExecutor(max_workers=2) as pool:
                tags_job = pool.submit(self.store.ai_tags, analysis_url)
                caption_job = pool.submit(self.store.ai_answer, analysis_url, [CAPTION_PROMPT])
            try:
                tags = tags_job.result()
                source = "cloudinary-ai-vision"
            except Exception as exc:
                errors.append(str(exc) if isinstance(exc, CloudError) else f"{type(exc).__name__}: {exc}")
                log.warning("AI Vision tagging failed, falling back to CLIP: %s", exc)
            try:
                answers = caption_job.result()
                caption = answers[0] if answers else None
            except Exception as exc:
                errors.append(str(exc) if isinstance(exc, CloudError) else f"{type(exc).__name__}: {exc}")
                log.warning("AI Vision caption failed: %s", exc)
            self.store.ai_vision_error = "; ".join(errors) or None
        if source == "none" and vector is not None:
            zs = self.embedder.zero_shot_tags(vector)
            if zs:
                tags = [t for t, _ in zs]
                source = "clip-zero-shot"
        return tags, caption, source

    def _fetch_frame(self, public_id: str):
        """Download a representative frame of a video (for embeddings and fingerprints)."""
        url = transforms.video_frame(public_id, 640, 480)
        for _ in range(3):
            try:
                r = httpx.get(url, timeout=30.0, follow_redirects=True)
                if r.status_code == 200 and r.content:
                    return open_image(r.content).convert("RGB")
            except Exception as exc:  # Cloudinary may still be preparing the frame
                log.debug("Video frame for %s not ready: %s", public_id, exc)
            time.sleep(2)
        return None

    def _find_reuse(self, evidence_id: str, sha: str, phash: Optional[str], dh: Optional[str], vector: Optional[List[float]],
                    captured_ts: float) -> Tuple[List[ReuseMatch], List[str]]:
        """Near-identical assets already on file.

        Returns (earlier, later_ids): matches captured before this one (this one is the copy)
        and ids of matches captured after it (those are the copies, to be re-checked).
        """
        matches: List[ReuseMatch] = []
        later: List[str] = []
        names = {p["id"]: p["name"] for p in self.db.projects()}
        for r in self.db.all_fingerprints():
            if r["id"] == evidence_id:
                continue
            how, dist = None, None
            if sha and r.get("sha256") == sha:
                how, dist = "same file", 0.0
            else:
                hp = hamming_hex(phash, r.get("phash"))
                hd = hamming_hex(dh, r.get("dhash"))
                bits = min(hp, hd)
                if bits <= 6:
                    how, dist = "near-identical image", float(bits)
                elif vector is not None and r.get("_vector") and self.embedder.is_clip:
                    sim = cosine(vector, r["_vector"])
                    if sim >= 0.975:
                        how, dist = "near-identical image", round(1 - sim, 4)
            if how:
                other_ts = float(r.get("captured_ts") or 0)
                if other_ts > captured_ts + 60:
                    later.append(r["id"])
                    continue
                matches.append(ReuseMatch(asset_id=r["id"], project_id=r.get("project_id") or "", project_name=names.get(r.get("project_id"), "another project"),
                                          site_id=r.get("site_id"), captured_at=r.get("captured_at"), how=how, distance=dist or 0.0))
        return matches, later

    def rescore(self, asset_id: str, reason: str = "") -> Optional[Dict[str, Any]]:
        """Re-run the integrity checks for one asset (e.g. when its original arrives later)."""
        a = self.db.asset(asset_id)
        if not a:
            return None
        reuse, _ = self._find_reuse(asset_id, a.get("sha256") or "", a.get("phash"), a.get("dhash"), a.get("_vector"),
                                    float(a.get("captured_ts") or 0))
        integ = score(IntegrityInput(
            has_exif=bool(a.get("has_exif")), has_gps=bool(a.get("has_gps")), lat=a.get("lat"), lon=a.get("lon"),
            captured_at=a.get("captured_at"), uploaded_at=a.get("uploaded_at") or _now_iso(), edited_with=a.get("edited_with"),
            quality=a.get("quality"), faces=int(a.get("faces") or 0), consent=bool(a.get("consent")),
            project=self.project(a.get("project_id")), site_id=a.get("site_id"), reuse=reuse, is_video=a.get("media_type") == "video",
        ))
        if integ.score == a.get("integrity_score") and integ.level == a.get("integrity_level"):
            return a
        self.db.update_asset(asset_id, integrity_score=integ.score, integrity_level=integ.level, integrity=integ.to_dict(),
                             reuse_of=_reuse_of(reuse, integ))
        self.store.update(a["public_id"], context={"integrity": str(integ.score), "integrity_level": integ.level,
                                                   **_checks_context(integ, _reuse_of(reuse, integ))},
                          tags=[f"integrity_{integ.level}"], resource_type=a.get("media_type") or "image")
        updated = self.db.asset(asset_id) or a
        if isinstance(self.index, QdrantIndex):
            self.index.enrich(asset_id, {"cloud": self.cloud_summary(updated)}, a.get("project_id"))
        self.db.event("integrity", f"Re-checked evidence {asset_id[:8]}: now {integ.level} ({integ.score}){' because ' + reason if reason else ''}",
                      a.get("project_id"), asset_id, "error" if integ.level == "flagged" else "info")
        if a.get("site_id"):
            self.refresh_pair(a["site_id"])
        return updated

    # ------------------------------------------------------------------ edits
    def edit(self, evidence_id: str, changes: Dict[str, Any]) -> Dict[str, Any]:
        a = self.db.asset(evidence_id)
        if not a:
            raise KeyError(evidence_id)
        upd: Dict[str, Any] = {}
        if "note" in changes and changes["note"] is not None:
            upd["note"] = str(changes["note"])[:2000]
        if "status_claim" in changes:
            upd["status_claim"] = changes["status_claim"] if changes["status_claim"] in STATUS_CLAIMS else None
        if "consent" in changes and changes["consent"] is not None:
            upd["consent"] = 1 if changes["consent"] else 0
        if upd:
            self.db.update_asset(evidence_id, **upd)
            ctx = {}
            if "note" in upd:
                ctx["note"] = upd["note"][:500]
            if "status_claim" in upd:
                ctx["status"] = upd["status_claim"] or ""
            if "consent" in upd:
                ctx["consent"] = "yes" if upd["consent"] else "no"
            self.store.update(a["public_id"], context=ctx)
            self.db.event("edit", f"Edited {', '.join(upd)} on evidence {evidence_id[:8]}", a["project_id"], evidence_id)
        return self.present(self.db.asset(evidence_id) or a)

    # ---------------------------------------------------------------- pairing
    def refresh_pair(self, site_id: str) -> Optional[Dict[str, Any]]:
        rows = [r for r in self.db.assets(site_id=site_id, limit=500) if (r.get("media_type") or "image") == "image"]
        cands = [pairing.PairCandidate(id=r["id"], captured_ts=r["captured_ts"] or 0, tags=r.get("ai_tags") or [],
                                       vector=r.get("_vector"), level=r.get("integrity_level") or "verified") for r in rows]
        best = pairing.best_pair(cands, min_gap_hours=self.s.pair_min_gap_hours)
        if not best:
            self.db.delete_auto_pair(site_id)
            return None
        existing = [p for p in self.db.pairs() if p["site_id"] == site_id and not p["manual"]]
        if existing and existing[0]["before_id"] == best.before_id and existing[0]["after_id"] == best.after_id:
            return existing[0]
        return self._save_pair(site_id, best, manual=False)

    def _save_pair(self, site_id: str, best: pairing.Pair, manual: bool) -> Dict[str, Any]:
        b = self.db.asset(best.before_id)
        a = self.db.asset(best.after_id)
        site = self.db.site(site_id)
        comp_url, comp_t = None, None
        if self.store.transformations and b and a:
            comp_url, comp_t = transforms.before_after(b["public_id"], a["public_id"], f"BEFORE {_short_date(b['captured_at'])}",
                                                       f"AFTER {_short_date(a['captured_at'])}")
        pid = self.db.save_pair({
            "project_id": (b or {}).get("project_id"), "site_id": site_id, "before_id": best.before_id, "after_id": best.after_id,
            "gap_days": best.gap_days, "similarity": best.similarity, "added": best.added, "removed": best.removed,
            "verdict": best.verdict, "summary": best.summary, "composite_url": comp_url, "composite_transform": comp_t,
            "change_text": None, "manual": 1 if manual else 0,
        })
        if comp_url:
            self.db.add_derivative("before_after", "Side-by-side composite", comp_t, comp_url, pair_id=pid)
        self.db.event("pair", f"Before/after at {site['name'] if site else site_id}: {best.summary}", (b or {}).get("project_id"))
        return self.db.pair(pid) or {}

    def manual_pair(self, before_id: str, after_id: str) -> Dict[str, Any]:
        b, a = self.db.asset(before_id), self.db.asset(after_id)
        if not b or not a:
            raise KeyError("both photos must exist")
        if (a["captured_ts"] or 0) < (b["captured_ts"] or 0):
            b, a = a, b
        d = pairing.tag_delta(b.get("ai_tags") or [], a.get("ai_tags") or [])
        v = pairing.verdict(d["added"], d["removed"])
        sim = cosine(b["_vector"], a["_vector"]) if b.get("_vector") and a.get("_vector") else None
        p = pairing.Pair(b["id"], a["id"], round(((a["captured_ts"] or 0) - (b["captured_ts"] or 0)) / 86400, 2),
                         round(sim, 4) if sim is not None else None, d["added"], d["removed"], v, pairing.describe(d["added"], d["removed"], v))
        return self._save_pair(b["site_id"] or f"manual-{b['id'][:8]}", p, manual=True)

    def describe_change(self, pair_id: int) -> Dict[str, Any]:
        p = self.db.pair(pair_id)
        if not p:
            raise KeyError(pair_id)
        if p.get("change_text"):
            return p
        if not (self.store.transformations and getattr(self.store, "ai_vision_enabled", False) and p.get("composite_url")):
            raise CloudError("Change descriptions need Cloudinary with the AI Vision add-on enabled")
        text = self.store.ai_answer(transforms.analysis_jpg(p["composite_url"]), [CHANGE_PROMPT])[0]
        self.db.x("UPDATE pairs SET change_text=? WHERE id=?", (text, pair_id))
        after = self.db.asset(p["after_id"])
        if after and after.get("public_id"):  # kept with the "after" photo, so a rebuild does not pay for it again
            self.store.update(after["public_id"], context={"change_text": (text or "")[:900], "change_before": p["before_id"]})
        self.db.event("ai", f"AI Vision described the change at {p['site_id']}", p["project_id"])
        return self.db.pair(pair_id) or p

    # --------------------------------------------------------------- campaign
    def campaign(self, caption: str, asset_id: Optional[str] = None, pair_id: Optional[int] = None) -> Dict[str, Any]:
        if not self.store.transformations:
            raise CloudError("Campaign images are rendered by Cloudinary; configure CLOUDINARY_URL")
        credit = f"{self.s.org_name.replace(' (demo NGO)', '')} · verified field evidence"
        out = []
        if pair_id is not None:
            p = self.db.pair(pair_id)
            if not p:
                raise KeyError(pair_id)
            b, a = self.db.asset(p["before_id"]), self.db.asset(p["after_id"])
            blur = not (b.get("consent") and a.get("consent"))
            url, t = transforms.before_after_stacked(b["public_id"], a["public_id"], f"BEFORE · {_short_date(b['captured_at'])}",
                                                     f"AFTER · {_short_date(a['captured_at'])}", blur_faces=blur)
            out.append({"key": "square_before_after", "name": "Before/after post (1080×1080)", "url": url, "transformation": t,
                        "download": transforms.attachment(url, "before-after"), "steps": transforms.explain(t)})
            self.db.add_derivative("campaign", "Before/after post", t, url, pair_id=pair_id)
            url, t = transforms.before_after(b["public_id"], a["public_id"], f"BEFORE {_short_date(b['captured_at'])}",
                                             f"AFTER {_short_date(a['captured_at'])}", w=600, h=630, blur_faces=blur)
            out.append({"key": "landscape_before_after", "name": "Before/after link preview (1200×630)", "url": url, "transformation": t,
                        "download": transforms.attachment(url, "before-after-wide"), "steps": transforms.explain(t)})
            self.db.add_derivative("campaign", "Before/after link preview", t, url, pair_id=pair_id)
            base = a
            if a.get("public_id"):  # remembered with the "after" photo, so a rebuild can show the same pack
                self.store.update(a["public_id"], context={"campaign_caption": caption[:300], "change_before": p["before_id"]})
        else:
            base = self.db.asset(asset_id or "")
            if not base:
                raise KeyError(asset_id)
            if base.get("media_type") == "video":
                raise CloudError("Campaign images are made from photos; pick a photo or a before/after pair")
        blur = not base.get("consent")
        for fmt in transforms.FORMATS:
            url, t = transforms.campaign_variant(base["public_id"], fmt, caption, credit, blur_faces=blur)
            out.append({"key": fmt["key"], "name": f"{fmt['name']} ({fmt['w']}×{fmt['h']})", "url": url, "transformation": t,
                        "download": transforms.attachment(url, fmt["key"]), "steps": transforms.explain(t)})
            self.db.add_derivative("campaign", fmt["name"], t, url, asset_id=base["id"])
        self.db.event("campaign", f"Campaign pack created ({len(out)} images)", base.get("project_id"), base["id"])
        return {"variants": out, "faces_blurred": blur}

    # ----------------------------------------------------------------- search
    def search(self, q: str, project_id: Optional[str] = None, limit: int = 40) -> Dict[str, Any]:
        q = (q or "").strip()
        t0 = time.perf_counter()
        dense = self.embedder.embed_text(q) if (q and self.embedder.text_matches_images) else None
        sparse = bm25_query(q) if q else None
        ranked: Dict[str, float] = {}
        for rank, (aid, _) in enumerate(self.index.search(dense, sparse, project_id, limit=limit * 2)):
            ranked[aid] = ranked.get(aid, 0.0) + 1.0 / (60 + rank)
        # keyword pass over captions, tags, notes (also covers the local index)
        words = [w for w in re.findall(r"[a-z0-9]+", q.lower()) if len(w) > 2]
        rows = self.db.assets(project_id=project_id, limit=5000)
        kw: List[Tuple[str, float]] = []
        for r in rows:
            hay = " ".join([r.get("caption") or "", r.get("note") or "", " ".join(r.get("ai_tags") or []),
                            " ".join(r.get("device_tags") or []), r.get("site_id") or ""]).lower().replace("_", " ")
            hits = sum(1 for w in words if w in hay)
            for t in taxonomy.TAGS:
                if any(w in t["keywords"] for w in words) and t["name"] in (r.get("ai_tags") or []):
                    hits += 1
            if hits:
                kw.append((r["id"], hits))
        kw.sort(key=lambda t: -t[1])
        for rank, (aid, _) in enumerate(kw[: limit * 2]):
            ranked[aid] = ranked.get(aid, 0.0) + 1.0 / (60 + rank)
        ids = sorted(ranked, key=lambda k: -ranked[k])[:limit]
        items = [self.present(a) for a in (self.db.asset(i) for i in ids) if a]
        mode = "semantic (CLIP) + keyword" if dense is not None else "keyword"
        if isinstance(self.index, QdrantIndex):
            mode += " · Qdrant"
        return {"query": q, "mode": mode, "latency_ms": round((time.perf_counter() - t0) * 1000, 1), "items": items}

    # ---------------------------------------------------------------- reports
    def report(self, project_id: str) -> Dict[str, Any]:
        p = self.project(project_id)
        if not p:
            raise KeyError(project_id)
        assets = self.db.assets(project_id=project_id, limit=5000)
        pairs = [self.present_pair(x) for x in self.db.pairs(project_id)]
        levels = {"verified": 0, "review": 0, "flagged": 0}
        tags: Dict[str, int] = {}
        days: Dict[str, int] = {}
        people = 0
        for a in assets:
            levels[a.get("integrity_level") or "review"] = levels.get(a.get("integrity_level") or "review", 0) + 1
            if a.get("integrity_level") == "flagged":
                continue  # flagged evidence is listed separately and never counted
            for t in a.get("ai_tags") or []:
                tags[t] = tags.get(t, 0) + 1
            d = (a.get("captured_at") or a.get("uploaded_at") or "")[:10]
            if d:
                days[d] = days.get(d, 0) + 1
            if a.get("faces") and not a.get("consent"):
                people += 1
        sites = self.db.sites(project_id)
        site_ids_with_evidence = {a["site_id"] for a in assets}
        improved = [x for x in pairs if x["verdict"] == "improved"]
        dates = sorted(days)
        kpis = {
            "evidence": len(assets),
            "sites": len([s for s in sites if s["id"] in site_ids_with_evidence]),
            "verified": levels["verified"],
            "review": levels["review"],
            "flagged": levels["flagged"],
            "verified_pct": round(100 * levels["verified"] / len(assets)) if assets else 0,
            "pairs": len(pairs),
            "improved": len(improved),
            "photos_with_people_blurred": people,
            "first_day": dates[0] if dates else None,
            "last_day": dates[-1] if dates else None,
            "devices": len({a.get("device_id") for a in assets}),
        }
        narrative = self._narrative(p, kpis, pairs, tags)
        return {
            "project": p.to_dict(),
            "organisation": self.s.org_name,
            "generated_at": _now_iso(),
            "kpis": kpis,
            "narrative": narrative,
            "pairs": pairs,
            "tags": sorted(({"tag": k, "label": taxonomy.label(k), "count": v} for k, v in tags.items()), key=lambda t: -t["count"]),
            "timeline": [{"day": d, "count": days[d]} for d in dates],
            "sites": [{**s, "evidence": sum(1 for a in assets if a["site_id"] == s["id"])} for s in sites if s["id"] in site_ids_with_evidence],
            "flagged": [self.present(a) for a in assets if a.get("integrity_level") == "flagged"],
            "highlights": [self.present(a) for a in sorted((a for a in assets if a.get("integrity_level") == "verified"),
                                                           key=lambda a: -(a.get("integrity_score") or 0))[:6]],
        }

    def _narrative(self, p: Project, k: Dict[str, Any], pairs: List[Dict[str, Any]], tags: Dict[str, int]) -> str:
        if not k["evidence"]:
            return f"No evidence has been recorded for {p.name} yet."
        first, last = _short_date(k["first_day"]), _short_date(k["last_day"])
        span = f"between {first} and {last}" if first != last else f"on {first}"
        top = ", ".join(taxonomy.label(t).lower() for t, _ in sorted(tags.items(), key=lambda t: -t[1])[:3])
        text = (f"Field teams recorded {k['evidence']} photos at {k['sites']} site{'s' if k['sites'] != 1 else ''} {span} for {p.name}. "
                f"{k['verified']} ({k['verified_pct']}%) passed every integrity check")
        text += (f"; {k['flagged']} {'was' if k['flagged'] == 1 else 'were'} flagged for review." if k["flagged"] else ".")
        if top:
            text += f" The most common visual signals were {top}."
        if pairs:
            text += f" Before/after comparisons exist for {k['pairs']} site{'s' if k['pairs'] != 1 else ''}"
            text += f", {k['improved']} showing visible improvement." if k["improved"] else "."
            best = next((x for x in pairs if x.get("change_text")), None) or next((x for x in pairs if x["verdict"] == "improved"), None)
            if best:
                detail = (best.get("change_text") or best["summary"]).strip().rstrip(".")
                text += f" At {best.get('site_name') or 'one site'}: {detail}."
        if k["photos_with_people_blurred"]:
            text += f" Faces in {k['photos_with_people_blurred']} photo(s) are blurred in every public output."
        return self._polish(text)

    def _polish(self, text: str) -> str:
        """Optional: rewrite the narrative with any OpenAI-compatible LLM. Falls back to the template."""
        if not (self.s.llm_base_url and self.s.llm_api_key and self.s.llm_model):
            return text
        try:
            r = httpx.post(
                self.s.llm_base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {self.s.llm_api_key}"},
                json={"model": self.s.llm_model, "temperature": 0.3, "messages": [
                    {"role": "system", "content": "Rewrite NGO impact report summaries in clear, warm, factual English. Keep every number. No new facts. Max 120 words."},
                    {"role": "user", "content": text}]},
                timeout=30.0,
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip() or text
        except Exception as exc:
            log.info("LLM polish skipped: %s", exc)
            return text

    # ------------------------------------------------------------- rebuild
    def rebuild_from_cloudinary(self) -> Dict[str, int]:
        """Recreate the index from Cloudinary tags and context (after a redeploy)."""
        if not isinstance(self.store, CloudinaryStore):
            return {"restored": 0}
        restored = 0
        contexts: Dict[str, Dict[str, str]] = {}
        # Only this deployment's folder: other folders in the account (a test setup, say) stay out
        for r in self.store.search_all(f'tags=saakshi AND public_id:"{self.s.folder}/*"'):
            ctx = (r.get("context") or {}).get("custom") or r.get("context") or {}
            eid = ctx.get("evidence_id")
            if not eid or self.db.asset(eid):
                continue
            contexts[eid] = ctx
            lat = float(ctx["lat"]) if ctx.get("lat") else None
            lon = float(ctx["lon"]) if ctx.get("lon") else None
            level = ctx.get("integrity_level") or "review"
            score_ = int(ctx.get("integrity") or 0)
            got = self.index.get(eid) if isinstance(self.index, QdrantIndex) else None
            self.db.insert_asset({
                "id": eid, "project_id": ctx.get("project") or None, "site_id": ctx.get("site") or None, "public_id": r["public_id"],
                "version": r.get("version"), "secure_url": r.get("secure_url"), "format": r.get("format"), "width": r.get("width"),
                "height": r.get("height"), "bytes": r.get("bytes"), "sha256": ctx.get("sha256"), "lat": lat, "lon": lon,
                "captured_at": ctx.get("captured_at") or None, "captured_ts": iso_to_ts(ctx.get("captured_at")) or 0,
                "uploaded_at": r.get("created_at"), "uploaded_ts": iso_to_ts(r.get("created_at")) or 0,
                "device_id": ctx.get("device"), "device_name": ctx.get("device_name") or ctx.get("device"), "source": ctx.get("source"),
                "note": ctx.get("note"), "status_claim": ctx.get("status") or None,
                "consent": 1 if ctx.get("consent") == "yes" else 0, "ai_tags": [t for t in (ctx.get("ai_tags") or "").split(";") if t],
                "caption": ctx.get("caption"), "vision_source": "cloudinary-ai-vision", "integrity_score": score_,
                "integrity_level": level, "integrity": {"score": score_, "level": level, "checks": _checks_from_context(ctx)},
                "reuse_of": ctx.get("reuse_of") or None, "faces": int(ctx.get("faces") or 0),
                "has_exif": 1 if ctx.get("has_exif") == "1" else 0, "edited_with": ctx.get("edited_with") or None,
                "quality": float(ctx["quality"]) if ctx.get("quality") else None, "phash": ctx.get("phash") or None,
                "dhash": ctx.get("dhash") or None,
                "has_gps": 1 if lat is not None else 0, "media_type": "video" if r.get("resource_type") == "video" else "image",
            }, got[0] if got else None)
            if ctx.get("site") and lat is not None and not self.db.site(ctx["site"]):
                self.db.upsert_site({"id": ctx["site"], "project_id": ctx.get("project") or "", "name": ctx.get("site_name") or ctx["site"],
                                     "lat": lat, "lon": lon, "auto": True})
            restored += 1
        for s in self.db.sites():
            self.refresh_pair(s["id"])
        # AI change descriptions were paid for once: put them back on the same pairs
        for p in self.db.pairs():
            ctx = contexts.get(p["after_id"]) or {}
            if not p.get("change_text") and ctx.get("change_text") and ctx.get("change_before") == p["before_id"]:
                self.db.x("UPDATE pairs SET change_text=? WHERE id=?", (ctx["change_text"], p["id"]))
            # campaign images are transformation URLs: rebuilding them costs nothing
            if ctx.get("campaign_caption") and ctx.get("change_before") == p["before_id"] and not any(d.get("purpose") == "campaign" for d in self.db.derivatives_for_pair(p["id"])):
                self.campaign(ctx["campaign_caption"], pair_id=p["id"])
        if restored:
            self.db.event("admin", f"Restored {restored} evidence records from Cloudinary")
        return {"restored": restored}

    # ------------------------------------------------------------- shaping
    def cloud_summary(self, a: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "public_id": a.get("public_id"),
            "secure_url": a.get("secure_url"),
            "thumb_url": (transforms.video_frame(a["public_id"]) if a.get("media_type") == "video" else self.store.url_thumb(a["public_id"]))
                         if a.get("public_id") else None,
            "integrity_score": a.get("integrity_score"),
            "integrity_level": a.get("integrity_level"),
            "ai_tags": a.get("ai_tags") or [],
            "caption": a.get("caption"),
            "vision_source": a.get("vision_source"),
        }

    def present(self, a: Dict[str, Any]) -> Dict[str, Any]:
        out = {k: v for k, v in a.items() if not k.startswith("_") and k != "raw"}
        pid = a.get("public_id")
        blur = bool(a.get("faces")) and not a.get("consent")
        if pid and a.get("media_type") == "video":
            out["thumb_url"] = transforms.video_frame(pid)
            out["public_thumb_url"] = transforms.video_frame(pid, blur_faces=blur)
            out["display_url"] = transforms.video_url(pid)
            out["public_display_url"] = out["display_url"]
            out["poster_url"] = transforms.video_frame(pid, 1280, 720)
            out["compare_url"] = transforms.video_frame(pid, 1000, 750)
        elif pid:
            out["thumb_url"] = self.store.url_thumb(pid)
            out["display_url"] = self.store.url_display(pid)
            out["public_thumb_url"] = self.store.url_thumb(pid, blur_faces=blur)
            out["public_display_url"] = self.store.url_display(pid, blur_faces=blur)
            out["compare_url"] = transforms.thumb(pid, 1000, 750, blur_faces=blur) if self.store.transformations else self.store.url_original(pid)
        site = self.db.site(a["site_id"]) if a.get("site_id") else None
        out["site_name"] = site["name"] if site else None
        proj = self.db.project(a["project_id"]) if a.get("project_id") else None
        out["project_name"] = proj["name"] if proj else None
        return out

    def present_pair(self, p: Dict[str, Any]) -> Dict[str, Any]:
        b, a = self.db.asset(p["before_id"]), self.db.asset(p["after_id"])
        site = self.db.site(p["site_id"]) if p.get("site_id") else None
        # a pair's campaign pack: the before/after posts, then the formats made from the "after" photo
        made = self.db.derivatives_for_pair(p["id"]) + self.db.q(
            "SELECT * FROM derivatives WHERE asset_id=? ORDER BY created_ts DESC", (p["after_id"],))
        campaign = [{"name": d["name"], "url": d["url"], "steps": transforms.explain(d.get("transformation") or "")}
                    for d in made if d.get("purpose") == "campaign"]
        return {**p, "site_name": site["name"] if site else p.get("site_id"),
                "before": self.present(b) if b else None, "after": self.present(a) if a else None, "campaign": campaign,
                "composite_steps": transforms.explain(p["composite_transform"]) if p.get("composite_transform") else []}

    def provenance(self, asset_id: str) -> Dict[str, Any]:
        a = self.db.asset(asset_id)
        if not a:
            raise KeyError(asset_id)
        derived = []
        for d in self.db.derivatives_for_asset(asset_id):
            derived.append({**d, "steps": transforms.explain(d.get("transformation") or "")})
        raw = a.get("raw") or {}
        meta = raw.get("media_metadata") or raw.get("image_metadata") or {}
        return {
            "original": {
                "public_id": a["public_id"], "version": a.get("version"), "secure_url": a.get("secure_url"), "bytes": a.get("bytes"),
                "format": a.get("format"), "sha256": a.get("sha256"), "phash": a.get("phash"), "asset_id": raw.get("asset_id"),
                "original_bytes": raw.get("original_bytes"),  # set when a smaller copy was stored
                "uploaded_at": a.get("uploaded_at"), "asset_folder": raw.get("asset_folder"),
            },
            "capture": {
                "device": a.get("device_name") or a.get("device_id"), "source": a.get("source"), "captured_at": a.get("captured_at"),
                "camera": a.get("camera"), "lat": a.get("lat"), "lon": a.get("lon"), "blurred_on_device": bool(a.get("blurred_on_device")),
            },
            "analysis": {"vision_source": a.get("vision_source"), "ai_tags": a.get("ai_tags"), "caption": a.get("caption"),
                         "device_tags": a.get("device_tags"), "quality": a.get("quality"), "faces": a.get("faces")},
            "metadata_keys": sorted(meta.keys())[:40] if isinstance(meta, dict) else [],
            "derived": derived,
        }

    def status(self) -> Dict[str, Any]:
        return {
            "org": self.s.org_name,
            "store": self.store.kind,
            "cloud_name": getattr(self.store, "cloud_name", None),
            "transformations": self.store.transformations,
            "ai_vision": bool(getattr(self.store, "ai_vision_enabled", False)),
            "ai_vision_error": getattr(self.store, "ai_vision_error", None),
            "ai_vision_quota": getattr(self.store, "last_quota", None),
            "index": self.index.kind,
            "embedder": {"mode": self.embedder.load(), "label": self.embedder.label},
            "counts": {"assets": len(self.db.assets(limit=100000)), "projects": len(self.db.projects())},
        }


def _short_date(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%d %b %Y")
    except ValueError:
        return iso[:10]


VIDEO_EXT = (".mp4", ".mov", ".m4v", ".3gp", ".webm", ".avi", ".mkv")


def _is_video(filename: str, data: bytes) -> bool:
    if (filename or "").lower().endswith(VIDEO_EXT):
        return True
    head = data[:16]
    return head[4:8] == b"ftyp" or head[:4] == b"\x1aE\xdf\xa3"  # MP4/MOV or Matroska/WebM


_DEG = re.compile(r"(-?\d+(?:\.\d+)?)\s*deg\s*(\d+(?:\.\d+)?)'\s*(\d+(?:\.\d+)?)\"?\s*([NSEW])?")


def _deg(text: str) -> Optional[float]:
    m = _DEG.search(text or "")
    if not m:
        try:
            return float(text)
        except (TypeError, ValueError):
            return None
    v = float(m.group(1)) + float(m.group(2)) / 60 + float(m.group(3)) / 3600
    return -v if m.group(4) in ("S", "W") else v


def _video_meta(meta: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    """Best-effort GPS and creation time from Cloudinary's media_metadata for a video."""
    lat = lon = None
    coords = meta.get("GPSCoordinates") or meta.get("GPSPosition")
    if isinstance(coords, str) and "," in coords:
        a, b = coords.split(",")[:2]
        lat, lon = _deg(a), _deg(b)
    if lat is None and meta.get("GPSLatitude"):
        lat, lon = _deg(str(meta.get("GPSLatitude"))), _deg(str(meta.get("GPSLongitude")))
    created = None
    for k in ("CreationDate", "CreateDate", "MediaCreateDate", "TrackCreateDate"):
        v = meta.get(k)
        if v and not str(v).startswith("0000"):
            try:
                created = datetime.strptime(str(v)[:19], "%Y:%m:%d %H:%M:%S").isoformat()
                break
            except ValueError:
                continue
    if lat is not None and lon is not None and abs(lat) <= 90 and abs(lon) <= 180:
        return lat, lon, created
    return None, None, created



def _checks_context(integ, reuse_of: Optional[str]) -> Dict[str, str]:
    """The integrity checks, compact enough for Cloudinary context, so a rebuilt dashboard still says why."""
    checks = json.dumps([[c.key, c.status, c.message] for c in integ.checks], separators=(",", ":"), ensure_ascii=False)
    if len(checks) > 1000:
        checks = json.dumps([[c.key, c.status, c.message[:60]] for c in integ.checks], separators=(",", ":"), ensure_ascii=False)
    return {"checks": checks, "reuse_of": reuse_of or ""}


def _checks_from_context(ctx: Dict[str, str]) -> List[Dict[str, str]]:
    try:
        return [{"key": k, "status": st, "message": msg} for k, st, msg in json.loads(ctx.get("checks") or "[]")]
    except (ValueError, TypeError):
        return []

def _reuse_of(reuse: List[ReuseMatch], integ) -> Optional[str]:
    """The earlier asset this one duplicates, when the reuse check raised a problem."""
    if reuse and any(c.key == "reuse" and c.status in ("fail", "warn") for c in integ.checks):
        return reuse[0].asset_id
    return None
