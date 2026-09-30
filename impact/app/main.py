"""HTTP API and web dashboard for Saakshi Impact."""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from saakshi_core.schema import STATUS_CLAIMS

from .cloud import CloudError, LocalStore
from .service import ImpactService, ImpactSettings, IngestInProgress

log = logging.getLogger("saakshi.impact.api")
STATIC = Path(__file__).parent / "static"


class ProjectBody(BaseModel):
    name: str
    activity: str = ""
    description: str = ""
    lat: float
    lon: float
    radius_m: float = 5000
    start_date: str
    end_date: str


class EditBody(BaseModel):
    note: Optional[str] = None
    status_claim: Optional[str] = None
    consent: Optional[bool] = None


class PairBody(BaseModel):
    before_id: str
    after_id: str


class SiteBody(BaseModel):
    name: str


class CampaignBody(BaseModel):
    caption: str = ""
    asset_id: Optional[str] = None
    pair_id: Optional[int] = None


def create_app(settings: ImpactSettings, service: Optional[ImpactService] = None) -> FastAPI:
    svc = service or ImpactService(settings)
    app = FastAPI(title="Saakshi Impact")
    app.state.service = svc

    def _check_token(token: Optional[str]) -> None:
        if settings.ingest_token and not secrets.compare_digest(token or "", settings.ingest_token):
            raise HTTPException(401, "Wrong or missing X-Saakshi-Token")

    def office_only(x_saakshi_token: Optional[str] = Header(default=None)) -> None:
        """Anything that changes evidence or spends Cloudinary quota needs the shared
        token (INGEST_TOKEN); reading the dashboard does not. Open when no token is set."""
        _check_token(x_saakshi_token)

    guarded = [Depends(office_only)]

    @app.on_event("startup")
    def _startup() -> None:
        def warm():
            svc.embedder.load()
            if not svc.db.assets(limit=1):
                try:
                    svc.rebuild_from_cloudinary()
                except Exception as exc:
                    log.warning("Rebuild from Cloudinary skipped: %s", exc)
        threading.Thread(target=warm, daemon=True).start()

    # ------------------------------------------------------------------ meta
    @app.get("/api/health")
    def health() -> Dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/status")
    def status() -> Dict[str, Any]:
        demo = {"video_url": settings.demo_video_url, "repo_url": settings.repo_url} if settings.public_demo else None
        return {**svc.status(), "office_key_required": bool(settings.ingest_token), "public_demo": demo}

    @app.get("/api/overview")
    def overview() -> Dict[str, Any]:
        projects = []
        for p in svc.projects():
            assets = svc.db.assets(project_id=p.id, limit=5000)
            projects.append({
                **p.to_dict(),
                "evidence": len(assets),
                "verified": sum(1 for a in assets if a.get("integrity_level") == "verified"),
                "flagged": sum(1 for a in assets if a.get("integrity_level") == "flagged"),
                "pairs": len(svc.db.pairs(p.id)),
                "cover": _cover(assets),
            })
        all_assets = svc.db.assets(limit=100000)
        return {
            "org": settings.org_name,
            "projects": projects,
            "totals": {
                "evidence": len(all_assets),
                "verified": sum(1 for a in all_assets if a.get("integrity_level") == "verified"),
                "review": sum(1 for a in all_assets if a.get("integrity_level") == "review"),
                "flagged": sum(1 for a in all_assets if a.get("integrity_level") == "flagged"),
                "devices": len({a.get("device_id") for a in all_assets if a.get("device_id")}),
                "from_field": sum(1 for a in all_assets if a.get("source") == "field"),
            },
            "flagged": [svc.present(a) for a in all_assets if a.get("integrity_level") == "flagged"][:12],
            "events": svc.db.events(limit=12),
            "status": svc.status(),
        }

    def _cover(assets):
        # a project's face is its best evidence: verified, else under review; a flagged photo only if nothing else
        by_level = {lvl: [a for a in assets if a.get("integrity_level") == lvl] for lvl in ("verified", "review")}
        pick = (by_level["verified"] or by_level["review"] or assets or [None])[0]
        return svc.present(pick)["public_thumb_url"] if pick else None

    # -------------------------------------------------------------- projects
    @app.get("/api/projects")
    def projects() -> Dict[str, Any]:
        return {"projects": [p.to_dict() for p in svc.projects()], "status_claims": STATUS_CLAIMS}

    @app.post("/api/projects", dependencies=guarded)
    def create_project(body: ProjectBody) -> Dict[str, Any]:
        return svc.create_project(body.model_dump())

    @app.get("/api/projects/{project_id}")
    def project(project_id: str) -> Dict[str, Any]:
        p = svc.project(project_id)
        if not p:
            raise HTTPException(404, "No such project")
        assets = [svc.present(a) for a in svc.db.assets(project_id=project_id, limit=5000)]
        return {
            "project": p.to_dict(),
            "assets": assets,
            "sites": [{**s, "evidence": sum(1 for a in assets if a["site_id"] == s["id"])} for s in svc.db.sites(project_id)],
            "pairs": [svc.present_pair(x) for x in svc.db.pairs(project_id)],
            "events": svc.db.events(project_id=project_id, limit=30),
        }

    @app.patch("/api/sites/{site_id}", dependencies=guarded)
    def rename_site(site_id: str, body: SiteBody) -> Dict[str, Any]:
        if not svc.db.site(site_id):
            raise HTTPException(404, "No such site")
        name = body.name.strip()[:80]
        if not name:
            raise HTTPException(400, "Give the site a name")
        svc.db.x("UPDATE sites SET name=? WHERE id=?", (name, site_id))
        svc.db.event("site", f"Site {site_id} renamed to {name}", (svc.db.site(site_id) or {}).get("project_id"))
        return svc.db.site(site_id) or {}

    @app.get("/api/projects/{project_id}/report")
    def report(project_id: str) -> Dict[str, Any]:
        try:
            return svc.report(project_id)
        except KeyError:
            raise HTTPException(404, "No such project") from None

    # ---------------------------------------------------------------- assets
    @app.get("/api/assets")
    def assets(project_id: Optional[str] = None, site_id: Optional[str] = None, level: Optional[str] = None,
               tag: Optional[str] = None, limit: int = 300) -> Dict[str, Any]:
        return {"items": [svc.present(a) for a in svc.db.assets(project_id, site_id, level, tag, limit)]}

    @app.get("/api/assets/{asset_id}")
    def asset(asset_id: str) -> Dict[str, Any]:
        a = svc.db.asset(asset_id)
        if not a:
            raise HTTPException(404, "No such evidence")
        pairs = [svc.present_pair(p) for p in svc.db.pairs(a.get("project_id")) if asset_id in (p["before_id"], p["after_id"])]
        similar = []
        if a.get("_vector"):
            from .vectors import cosine_many

            others = [r for r in svc.db.assets(project_id=a.get("project_id"), limit=2000) if r["id"] != asset_id]
            sims = cosine_many(a["_vector"], others)
            for r in sorted(others, key=lambda r: -sims.get(r["id"], 0))[:6]:
                similar.append({**svc.present(r), "similarity": round(sims.get(r["id"], 0), 3)})
        return {"asset": svc.present(a), "provenance": svc.provenance(asset_id), "pairs": pairs, "similar": similar}

    @app.patch("/api/evidence/{evidence_id}", dependencies=guarded)
    def edit(evidence_id: str, body: EditBody) -> Dict[str, Any]:
        try:
            return svc.edit(evidence_id, body.model_dump(exclude_none=True))
        except KeyError:
            raise HTTPException(404, "No such evidence") from None

    # ---------------------------------------------------------------- ingest
    @app.post("/api/upload", dependencies=guarded)
    async def upload(files: List[UploadFile] = File(...), project_id: str = Form(""), note: str = Form(""),
                     status_claim: str = Form(""), consent: bool = Form(False)) -> Dict[str, Any]:
        results = []
        for f in files:
            data = await f.read()
            if not data:
                continue
            try:
                # ingest blocks for seconds (Cloudinary, AI Vision): keep the event loop free for other requests
                r = await run_in_threadpool(svc.ingest, data, f.filename or "photo.jpg", {"project_id": project_id or None, "note": note,
                                                                                         "status_claim": status_claim or None, "consent": consent}, "web")
                results.append(r["asset"])
            except CloudError as exc:
                results.append({"file_name": f.filename, "error": str(exc)})
            except Exception as exc:
                log.exception("upload failed")
                results.append({"file_name": f.filename, "error": f"Could not process this image: {exc}"})
        return {"results": results}

    @app.post("/api/ingest")
    async def ingest(response: Response, file: UploadFile = File(...), meta: str = Form("{}"),
                     x_saakshi_token: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        """Endpoint the field devices sync photos to."""
        _check_token(x_saakshi_token)
        try:
            m = json.loads(meta or "{}")
        except ValueError:
            raise HTTPException(400, "meta must be JSON") from None
        data = await file.read()
        t0 = time.perf_counter()
        try:
            out = await run_in_threadpool(svc.ingest, data, m.get("file_name") or file.filename or "photo.jpg", m, "field")
            # Our own processing time, so the device can tell it apart from time on the network
            response.headers["Server-Timing"] = f"ingest;dur={(time.perf_counter() - t0) * 1000:.0f}"
            return out
        except IngestInProgress as exc:
            raise HTTPException(409, str(exc)) from exc
        except CloudError as exc:
            log.warning("Ingest of %s failed: %s", m.get("file_name") or file.filename, exc)
            raise HTTPException(502, str(exc)) from exc

    # ---------------------------------------------------------------- pairs
    @app.post("/api/pairs", dependencies=guarded)
    def manual_pair(body: PairBody) -> Dict[str, Any]:
        try:
            return svc.present_pair(svc.manual_pair(body.before_id, body.after_id))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/pairs/{pair_id}/describe", dependencies=guarded)
    def describe(pair_id: int) -> Dict[str, Any]:
        try:
            return svc.present_pair(svc.describe_change(pair_id))
        except KeyError:
            raise HTTPException(404, "No such pair") from None
        except CloudError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/campaign", dependencies=guarded)
    def campaign(body: CampaignBody) -> Dict[str, Any]:
        try:
            return svc.campaign(body.caption, asset_id=body.asset_id, pair_id=body.pair_id)
        except KeyError:
            raise HTTPException(404, "Pick a photo or a before/after pair first") from None
        except CloudError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/search")
    def search(q: str = "", project_id: Optional[str] = None) -> Dict[str, Any]:
        return svc.search(q, project_id or None)

    @app.get("/api/events")
    def events(project_id: Optional[str] = None) -> Dict[str, Any]:
        return {"items": svc.db.events(project_id, 60)}

    @app.post("/api/admin/rebuild", dependencies=guarded)
    def rebuild() -> Dict[str, Any]:
        return svc.rebuild_from_cloudinary()

    @app.get("/media/{name}")
    def media(name: str):
        if not isinstance(svc.store, LocalStore):
            raise HTTPException(404)
        p = svc.store.file_for(name)
        if not p:
            raise HTTPException(404)
        return FileResponse(p, media_type="image/jpeg")

    app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")
    return app
