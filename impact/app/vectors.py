"""Vector index for the cloud app.

With QDRANT_URL set, the cloud app shares the `saakshi_evidence` collection
with the field devices: points pushed by a device are reused (no second
embedding), photos uploaded on the web are added, and the cloud writes its
enrichment (AI tags, caption, integrity) back into each point's payload so
devices receive it on their next partial snapshot.

Without Qdrant, vectors are kept in SQLite and searched with numpy.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from saakshi_core.qdrant_server import ServerCollection, point_struct, shard_key_for
from saakshi_core.schema import VECTOR_BM25, VECTOR_CLIP

log = logging.getLogger("saakshi.impact.vectors")


class QdrantIndex:
    kind = "qdrant"

    def __init__(self, url: str, api_key: Optional[str], collection: str):
        self.coll = ServerCollection(url, api_key, collection)
        self.client = self.coll.client
        self.collection = collection
        self._ready = False
        self.url = url

    def ready(self) -> bool:
        if not self._ready:
            try:
                self.coll.ensure()
                self._ready = True
            except Exception as exc:
                log.warning("Qdrant not reachable: %s", exc)
        return self._ready

    @property
    def layout(self) -> Optional[str]:
        return self.coll.layout if self.ready() else None

    def get(self, point_id: str) -> Optional[Tuple[Optional[List[float]], Dict[str, Any]]]:
        if not self.ready():
            return None
        try:
            recs = self.coll.retrieve([point_id], with_vectors=[VECTOR_CLIP])
        except Exception:
            return None
        if not recs:
            return None
        vec = recs[0].vector or {}
        clip = vec.get(VECTOR_CLIP) if isinstance(vec, dict) else None
        return (list(clip) if clip is not None else None), dict(recs[0].payload or {})

    def upsert(self, point_id: str, clip: List[float], sparse: Tuple[List[int], List[float]], payload: Dict[str, Any]) -> None:
        if not self.ready():
            return
        p = point_struct(point_id, clip, {"indices": sparse[0], "values": sparse[1]}, payload)
        self.coll.upsert([p], {point_id: shard_key_for(payload.get("project_id"))})

    def enrich(self, point_id: str, fields: Dict[str, Any], project_id: Optional[str] = None) -> None:
        """Write cloud results into an existing point; devices pick them up on the next pull."""
        if not self.ready():
            return
        try:
            # The point lives in the shard of the project the device gave it; ask the point, not our guess
            if self.coll.layout != "global":
                recs = self.coll.retrieve([point_id])
                if recs:
                    project_id = (recs[0].payload or {}).get("project_id")
            self.coll.set_payload(point_id, {**fields, "updated_ts": time.time()}, shard_key_for(project_id))
        except Exception as exc:
            log.warning("Could not enrich point %s: %s", point_id, exc)

    def search(self, dense: Optional[List[float]], sparse: Optional[Tuple[List[int], List[float]]], project_id: Optional[str], limit: int = 50) -> List[Tuple[str, float]]:
        if not self.ready():
            return []
        from qdrant_client import models

        flt = None
        conds = [models.FieldCondition(key="kind", match=models.MatchValue(value="evidence"))]
        if project_id:
            conds.append(models.FieldCondition(key="project_id", match=models.MatchValue(value=project_id)))
        flt = models.Filter(must=conds)
        prefetch = []
        if dense is not None:
            prefetch.append(models.Prefetch(query=list(dense), using=VECTOR_CLIP, limit=limit * 2, filter=flt))
        if sparse and sparse[0]:
            prefetch.append(models.Prefetch(query=models.SparseVector(indices=list(sparse[0]), values=list(sparse[1])), using=VECTOR_BM25, limit=limit * 2, filter=flt))
        if not prefetch:
            return []
        try:
            if not self.coll.has_shards():
                return []  # nothing synced yet
            if len(prefetch) == 1:
                res = self.client.query_points(self.collection, query=prefetch[0].query, using=prefetch[0].using, query_filter=flt, limit=limit)
            else:
                res = self.client.query_points(self.collection, prefetch=prefetch, query=models.FusionQuery(fusion=models.Fusion.RRF), limit=limit)
        except Exception as exc:  # the caller still runs its keyword search over captions, tags and notes
            log.warning("Qdrant search failed, keyword results only: %s", exc)
            return []
        return [(str(p.id), float(p.score)) for p in res.points]


class LocalIndex:
    kind = "local"

    def __init__(self, db):
        self.db = db

    def ready(self) -> bool:
        return True

    def get(self, point_id: str):
        return None

    def upsert(self, point_id, clip, sparse, payload) -> None:
        return None  # vectors live in the assets table

    def enrich(self, point_id, fields) -> None:
        return None

    def search(self, dense: Optional[List[float]], sparse, project_id: Optional[str], limit: int = 50) -> List[Tuple[str, float]]:
        if dense is None:
            return []
        rows = self.db.assets(project_id=project_id, limit=5000)
        ids, mats = [], []
        for r in rows:
            v = r.get("_vector")
            if v:
                ids.append(r["id"])
                mats.append(v)
        if not mats:
            return []
        m = np.asarray(mats, dtype=np.float32)
        q = np.asarray(dense, dtype=np.float32)
        sims = m @ q / (np.linalg.norm(m, axis=1) * (np.linalg.norm(q) or 1.0) + 1e-9)
        order = np.argsort(-sims)[:limit]
        return [(ids[i], float(sims[i])) for i in order]


def cosine_many(query: Sequence[float], rows: List[Dict[str, Any]]) -> Dict[str, float]:
    q = np.asarray(query, dtype=np.float32)
    qn = float(np.linalg.norm(q)) or 1.0
    out = {}
    for r in rows:
        v = r.get("_vector")
        if not v:
            continue
        a = np.asarray(v, dtype=np.float32)
        out[r["id"]] = float(a @ q / ((float(np.linalg.norm(a)) or 1.0) * qn))
    return out
