"""HTTP API and web UI for Saakshi Field (runs on the device, on localhost)."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from saakshi_core.config import REPO_ROOT
from saakshi_core.schema import STATUS_CLAIMS

from .assistant import Assistant
from .service import FieldService, FieldSettings
from .sync import SyncEngine

log = logging.getLogger("saakshi.field")
STATIC = Path(__file__).parent / "static"
# Shown for a colleague's photo whose thumbnail is not on this device yet (offline, before it was fetched)
NO_THUMB_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 480 360"><rect width="480" height="360" fill="#DCE3DD"/>'
    '<text x="240" y="186" font-family="system-ui,sans-serif" font-size="20" fill="#5A6760" text-anchor="middle">'
    "Photo not on this device yet</text></svg>"
)
BENCH_RESULTS = REPO_ROOT / "docs" / "benchmarks" / "results.json"


class EditBody(BaseModel):
    note: Optional[str] = None
    status_claim: Optional[str] = None
    consent: Optional[bool] = None
    private: Optional[bool] = None


class ResolveBody(BaseModel):
    choice: str  # "local", "remote" or "custom"
    status: Optional[str] = None


class NetworkBody(BaseModel):
    offline: Optional[bool] = None
    mode: Optional[str] = None  # online | slow | offline


class SettingsBody(BaseModel):
    auto_sync: Optional[bool] = None
    duplicate_threshold: Optional[float] = None
    privacy_mode: Optional[str] = None
    metered: Optional[bool] = None
    auto_bandwidth: Optional[bool] = None
    media_budget_mb: Optional[float] = None


class ApproveBody(BaseModel):
    mode: str = "blur"  # "blur" or "consent"


class AskBody(BaseModel):
    question: str
    lat: Optional[float] = None
    lon: Optional[float] = None


class RegionsBody(BaseModel):
    regions: List[str]


def create_app(settings: FieldSettings, start_sync: bool = True) -> FastAPI:
    service = FieldService(settings)
    engine = SyncEngine(service)
    assistant = Assistant(service)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        service.embedder.load()
        if start_sync:
            engine.start()
        service.db.log("device", f"{settings.device_name} started ({service.embedder.label})")
        yield
        engine.stop()
        service.memory.flush()
        service.memory.close()

    app = FastAPI(title=f"Saakshi Field · {settings.device_name}", lifespan=lifespan)
    app.state.service = service
    app.state.engine = engine

    # ------------------------------------------------------------- status
    @app.get("/api/status")
    def status() -> Dict[str, Any]:
        return {**service.stats(), "sync": engine.status(), "policy": service.s.policy.__dict__, "status_claims": STATUS_CLAIMS}

    @app.get("/api/system")
    def system() -> Dict[str, Any]:
        """Everything the live system view draws."""
        return {
            "device": {"id": settings.device_id, "name": settings.device_name, "memory": service.memory.stats(), "storage": service.storage(),
                       "embedder": service.embedder.label},
            "outbox": service.db.outbox_counts(),
            "sync": engine.status(),
            "server": engine.server_summary() if settings.qdrant_url else {"reachable": False, "configured": False},
            "regions": {"subscribed": service.subscribed_regions()},
        }

    @app.get("/api/projects")
    def projects() -> Dict[str, Any]:
        return {"projects": [p.to_dict() for p in service.projects()]}

    # ------------------------------------------------------------ evidence
    @app.post("/api/evidence")
    async def import_evidence(
        files: List[UploadFile] = File(...),
        note: str = Form(""),
        project_id: str = Form(""),
        status_claim: str = Form(""),
        consent: bool = Form(False),
        private: bool = Form(False),
    ) -> Dict[str, Any]:
        results = []
        for f in files:
            data = await f.read()
            if not data:
                continue
            try:
                # embedding and face detection take a while: keep the event loop free for search and status
                results.append(await run_in_threadpool(
                    service.ingest, data, f.filename or "photo.jpg", note=note, project_id=project_id or None,
                    status_claim=status_claim or None, consent=consent, private=private,
                ))
            except Exception as exc:
                log.exception("ingest failed")
                results.append({"file_name": f.filename, "error": f"Could not read this image: {exc}"})
        return {"results": results}

    @app.get("/api/evidence")
    def list_evidence(project_id: Optional[str] = None, include_mirror: bool = True) -> Dict[str, Any]:
        return {"items": service.list_evidence(include_mirror=include_mirror, project_id=project_id)}

    @app.get("/api/evidence/{evidence_id}")
    def get_evidence(evidence_id: str) -> Dict[str, Any]:
        item = service.get(evidence_id)
        if not item:
            raise HTTPException(404, "No evidence with this id on this device")
        return item

    @app.patch("/api/evidence/{evidence_id}")
    def edit_evidence(evidence_id: str, body: EditBody) -> Dict[str, Any]:
        try:
            return service.edit(evidence_id, body.model_dump(exclude_none=True))
        except KeyError:
            raise HTTPException(404, "No evidence with this id on this device") from None
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc

    @app.post("/api/evidence/{evidence_id}/approve")
    def approve(evidence_id: str, body: ApproveBody) -> Dict[str, Any]:
        try:
            return service.approve_held(evidence_id, body.mode)
        except KeyError:
            raise HTTPException(404, "No evidence with this id on this device") from None

    @app.get("/api/evidence/{evidence_id}/similar")
    def similar(evidence_id: str) -> Dict[str, Any]:
        return {"items": service.similar(evidence_id)}

    @app.get("/api/evidence/{evidence_id}/more-like-this")
    def more_like_this(evidence_id: str, exclude: str = "") -> Dict[str, Any]:
        not_ids = [x for x in exclude.split(",") if x]
        return {"items": service.more_like_this(evidence_id, not_ids)}

    @app.get("/api/search")
    def search(
        q: str = "",
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
        return service.search(q, project_id=project_id or None, limit=limit, recency_hours=recency_hours, near_site=near_site or None,
                              lat=lat, lon=lon, radius_km=radius_km, boost_near=boost_near, diverse=diverse,
                              verified_only=verified_only, status=status or None, source=source)

    @app.post("/api/ask")
    def ask(body: AskBody) -> Dict[str, Any]:
        return assistant.ask(body.question, body.lat, body.lon)

    # --------------------------------------------------------------- media
    @app.get("/media/thumb/{evidence_id}")
    def thumb(evidence_id: str):
        p = service.thumb_path(evidence_id)
        if not p.exists() and engine.is_online():
            # A colleague's photo that arrived in a snapshot: fetch its thumbnail once and keep it for offline use
            item = service.get(evidence_id)
            if item and item.get("source") != "local":
                engine.cache_remote_thumb(evidence_id, item)
        if p.exists():
            return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})
        return Response(NO_THUMB_SVG, media_type="image/svg+xml", headers={"Cache-Control": "no-store"})

    @app.get("/media/original/{evidence_id}")
    def original(evidence_id: str):
        item = service.get(evidence_id)
        if not item or item.get("source") != "local":
            raise HTTPException(404, "Original is only stored on the device that captured it")
        if item.get("media_evicted"):
            cloud = (item.get("cloud") or {}).get("secure_url")
            return JSONResponse({"detail": "The original was released from this device to save space; it is safe in the cloud.",
                                 "cloud_url": cloud}, status_code=410)
        p = service.media_path(evidence_id, item.get("file_ext", ".jpg"))
        if not p.exists():
            raise HTTPException(404, "Original file missing")
        return FileResponse(p)

    # ---------------------------------------------------------------- sync
    @app.get("/api/outbox")
    def outbox(include_done: bool = False) -> Dict[str, Any]:
        rows = service.db.outbox(include_done=include_done)
        for r in rows:
            item = service.get(r["evidence_id"])
            r["label"] = (item or {}).get("file_name") or (item or {}).get("note") or r["evidence_id"][:8]
        return {"items": rows, "counts": service.db.outbox_counts()}

    @app.post("/api/sync/now")
    def sync_now() -> Dict[str, Any]:
        if not engine.is_online():
            return {"ok": False, "message": "Offline: items stay in the outbox until the network is back"}
        return {"ok": True, **engine.sync_now()}

    @app.post("/api/network")
    def network(body: NetworkBody) -> Dict[str, Any]:
        mode = body.mode or ("offline" if body.offline else "online")
        try:
            engine.set_network_mode(mode)
        except ValueError:
            raise HTTPException(400, "mode must be online, slow or offline") from None
        return engine.status()

    @app.post("/api/settings")
    def settings_(body: SettingsBody) -> Dict[str, Any]:
        if body.auto_sync is not None:
            service.db.set("auto_sync", body.auto_sync)
        changes = body.model_dump(exclude_none=True)
        changes.pop("auto_sync", None)
        policy = service.save_policy(changes) if changes else service.s.policy
        return {"auto_sync": service.db.get("auto_sync", True), "policy": policy.__dict__, "storage": service.storage()}

    @app.get("/api/regions")
    def regions() -> Dict[str, Any]:
        return {"subscribed": service.subscribed_regions(), "available": [{"id": p.id, "name": p.name} for p in service.projects()] +
                [{"id": "unassigned", "name": "Photos without a project"}], "mirrors": service.memory.stats()["mirrors"]}

    @app.post("/api/regions")
    def set_regions(body: RegionsBody) -> Dict[str, Any]:
        return {"subscribed": service.set_regions(body.regions)}

    # ----------------------------------------------------------- conflicts
    @app.get("/api/conflicts")
    def conflicts() -> Dict[str, Any]:
        items = [{**c, "site_name": service.site_name(c["site_id"])} for c in service.db.conflicts()]
        sites = [{**x, "site_name": service.site_name(x["site_id"])} for x in service.db.site_states()]
        return {"items": items, "sites": sites}

    @app.post("/api/conflicts/{conflict_id}/resolve")
    def resolve(conflict_id: int, body: ResolveBody) -> Dict[str, Any]:
        try:
            return service.resolve_conflict(conflict_id, body.choice, body.status)
        except KeyError:
            raise HTTPException(404, "No such conflict") from None

    @app.get("/api/sites")
    def sites() -> Dict[str, Any]:
        return {"items": service.sites_overview()}

    @app.get("/api/sites/{site_id}/timeline")
    def timeline(site_id: str) -> Dict[str, Any]:
        return service.site_timeline(site_id)

    @app.get("/api/activity")
    def activity(limit: int = 100) -> Dict[str, Any]:
        return {"items": service.db.activity(limit)}

    # ---------------------------------------------------------- benchmarks
    @app.post("/api/benchmark/live")
    def live_benchmark() -> Dict[str, Any]:
        from bench.live import run_live

        return run_live(service, engine)

    @app.get("/api/benchmark/results")
    def bench_results() -> Dict[str, Any]:
        if BENCH_RESULTS.exists():
            return json.loads(BENCH_RESULTS.read_text(encoding="utf-8"))
        return {"available": False}

    @app.get("/api/health")
    def health() -> Dict[str, str]:
        return {"status": "ok", "device": settings.device_id}

    app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")
    return app
