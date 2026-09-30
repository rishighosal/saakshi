"""Server-side Qdrant helpers shared by the sync engine and the cloud app.

The server collection mirrors the edge shard layout exactly (same named
vectors) so that a shard snapshot can be unpacked straight into an edge shard
with `EdgeShard.unpack_snapshot`.

Two layouts, decided once when the collection is created:

* **regional** (custom sharding, one shard key per project): used when the
  Qdrant server runs in cluster mode, which includes Qdrant Cloud and the
  repo's docker-compose. Each device downloads only the shards of the projects
  it serves, and partial snapshots carry only that project's changes.
* **global** (one auto shard): used with a plain standalone server. Every
  device mirrors the whole collection.

Every component reads the layout from the collection itself, so they always
agree.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, Iterable, List, Optional

import httpx

from .schema import CLIP_DIM, FLOAT_INDEXES, GEO_INDEX, KEYWORD_INDEXES, VECTOR_BM25, VECTOR_CLIP

log = logging.getLogger("saakshi.qdrant")

GLOBAL = "global"
REGIONAL = "regional"
UNASSIGNED = "unassigned"   # shard key for evidence without a project


def make_client(url: str, api_key: Optional[str] = None, timeout: float = 10.0):
    from qdrant_client import QdrantClient

    return QdrantClient(url=url, api_key=api_key or None, timeout=int(timeout))


def _headers(api_key: Optional[str]) -> Dict[str, str]:
    return {"api-key": api_key} if api_key else {}


def cluster_enabled(url: str, api_key: Optional[str] = None) -> bool:
    try:
        r = httpx.get(url.rstrip("/") + "/cluster", headers=_headers(api_key), timeout=5.0)
        return r.status_code == 200 and r.json().get("result", {}).get("status") == "enabled"
    except Exception:
        return False


def shard_key_for(project_id: Optional[str]) -> str:
    return (project_id or UNASSIGNED).strip() or UNASSIGNED


# Partial snapshots transfer whole segments, so the server layout decides how
# much a device downloads after a change (measured in bench/bench_sync.py).
# A regional shard is small, and with the default settings it is one segment:
# any new point re-sends the whole region. With a low indexing threshold the
# filled segments are sealed (indexed, non-appendable) quickly and new evidence
# lands in a small fresh segment, so an update is ~0.5 MB instead of ~5 MB.
# The global layout is left on Qdrant's defaults, which already behave well
# for one large shard (more segments there make the merge optimizer rewrite
# old segments and partial snapshots grow).
# Same graph density as the device (field/app/memory.py): a device's mirror of a
# region is the server's shard, so the server's HNSW settings are the device's too.
HNSW_M = 32
HNSW_EF_CONSTRUCT = 256


def optimizer_config(regional: bool):
    from qdrant_client import models

    segs = os.environ.get("SAAKSHI_SEGMENTS_PER_SHARD")
    thr = os.environ.get("SAAKSHI_INDEXING_THRESHOLD_KB")
    if not regional and not segs and not thr:
        return None
    return models.OptimizersConfigDiff(
        default_segment_number=int(segs or 4),
        indexing_threshold=int(thr or 500),
    )


class ServerCollection:
    """One Saakshi evidence collection on a Qdrant server, in either layout."""

    def __init__(self, url: str, api_key: Optional[str], name: str, prefer_regional: bool = True):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.name = name
        self.prefer_regional = prefer_regional
        self.client = make_client(url, api_key)
        self._layout: Optional[str] = None
        self._keys: set = set()
        self._lock = threading.Lock()

    # ----------------------------------------------------------------- setup
    @property
    def layout(self) -> str:
        if self._layout is None:
            self.ensure()
        return self._layout or GLOBAL

    def ensure(self) -> bool:
        """Create the collection if missing. Returns True when created."""
        from qdrant_client import models

        with self._lock:
            if self.client.collection_exists(self.name):
                if self._layout is None:
                    info = httpx.get(f"{self.url}/collections/{self.name}", headers=_headers(self.api_key), timeout=10.0).json()
                    method = info.get("result", {}).get("config", {}).get("params", {}).get("sharding_method")
                    self._layout = REGIONAL if method == "custom" else GLOBAL
                    if self._layout == REGIONAL:
                        self._keys = set(self.shard_ids().keys())
                return False
            regional = self.prefer_regional and cluster_enabled(self.url, self.api_key)
            kwargs: Dict[str, Any] = dict(
                collection_name=self.name,
                vectors_config={VECTOR_CLIP: models.VectorParams(size=CLIP_DIM, distance=models.Distance.COSINE)},
                sparse_vectors_config={VECTOR_BM25: models.SparseVectorParams(modifier=models.Modifier.IDF)},
                shard_number=1,
                hnsw_config=models.HnswConfigDiff(m=HNSW_M, ef_construct=HNSW_EF_CONSTRUCT),
                optimizers_config=optimizer_config(regional),
            )
            if regional:
                kwargs["sharding_method"] = models.ShardingMethod.CUSTOM
            self.client.create_collection(**kwargs)
            for field in KEYWORD_INDEXES:
                self.client.create_payload_index(self.name, field, models.PayloadSchemaType.KEYWORD)
            for field in FLOAT_INDEXES:
                self.client.create_payload_index(self.name, field, models.PayloadSchemaType.FLOAT)
            self.client.create_payload_index(self.name, GEO_INDEX, models.PayloadSchemaType.GEO)
            self._layout = REGIONAL if regional else GLOBAL
            log.info("Created Qdrant collection %s (%s layout)", self.name, self._layout)
            return True

    def ensure_key(self, key: str) -> None:
        if self.layout != REGIONAL or key in self._keys:
            return
        with self._lock:
            if key in self._keys:
                return
            try:
                self.client.create_shard_key(self.name, key)
            except Exception as exc:
                if "already exists" not in str(exc) and "exist" not in str(exc).lower():
                    raise
            self._keys.add(key)

    def has_shards(self) -> bool:
        """False while a regional collection has no shard key yet (a fresh deployment,
        before the first photo). Qdrant 1.19 drops queries on such a collection without
        a response, so callers skip them. Keys are created by devices in other
        processes, so an empty cache is re-checked with the server."""
        if self.layout != REGIONAL:
            return True
        if not self._keys:
            self._keys = set(self.shard_ids())
        return bool(self._keys)

    def shard_ids(self) -> Dict[str, int]:
        """shard key -> shard id (regional layout); {"_all": 0} for global."""
        if self._layout == GLOBAL:
            return {"_all": 0}
        r = httpx.get(f"{self.url}/collections/{self.name}/cluster", headers=_headers(self.api_key), timeout=10.0)
        r.raise_for_status()
        res = r.json()["result"]
        out: Dict[str, int] = {}
        for s in res.get("local_shards", []) + res.get("remote_shards", []):
            key = s.get("shard_key")
            if key is not None:
                out[str(key)] = int(s["shard_id"])
        return out

    # ---------------------------------------------------------------- writes
    def upsert(self, points: Iterable[Any], key_of: Dict[str, str]) -> None:
        """Upsert PointStructs; `key_of` maps point id -> shard key (regional layout)."""
        points = list(points)
        if not points:
            return
        if self.layout == GLOBAL:
            self.client.upsert(self.name, points=points, wait=True)
            return
        groups: Dict[str, List[Any]] = {}
        for p in points:
            groups.setdefault(key_of.get(str(p.id), UNASSIGNED), []).append(p)
        for key, group in groups.items():
            self.ensure_key(key)
            self.client.upsert(self.name, points=group, wait=True, shard_key_selector=key)

    def set_payload(self, point_id: str, payload: Dict[str, Any], key: str) -> None:
        if self.layout == GLOBAL:
            self.client.set_payload(self.name, payload=payload, points=[point_id], wait=True)
        else:
            self.ensure_key(key)
            self.client.set_payload(self.name, payload=payload, points=[point_id], wait=True, shard_key_selector=key)

    # ----------------------------------------------------------------- reads
    def retrieve(self, ids: List[str], with_vectors: Any = False):
        return self.client.retrieve(self.name, ids=ids, with_payload=True, with_vectors=with_vectors)

    def count(self) -> Optional[int]:
        try:
            return int(self.client.count(self.name, exact=True).count)
        except Exception:
            return None

    def snapshot_url(self, shard_id: int) -> str:
        return f"{self.url}/collections/{self.name}/shards/{shard_id}/snapshot"


def point_struct(point_id: str, clip: Optional[List[float]], sparse: Optional[Dict[str, List]], payload: Dict[str, Any]):
    from qdrant_client import models

    vector: Dict[str, Any] = {}
    if clip is not None:
        vector[VECTOR_CLIP] = list(clip)
    if sparse and sparse.get("indices"):
        vector[VECTOR_BM25] = models.SparseVector(indices=list(sparse["indices"]), values=list(sparse["values"]))
    return models.PointStruct(id=point_id, vector=vector, payload=payload)


def ensure_collection(client, name: str) -> bool:
    """Backwards-compatible helper: global layout collection."""
    from qdrant_client import models

    if client.collection_exists(name):
        return False
    client.create_collection(
        collection_name=name,
        vectors_config={VECTOR_CLIP: models.VectorParams(size=CLIP_DIM, distance=models.Distance.COSINE)},
        sparse_vectors_config={VECTOR_BM25: models.SparseVectorParams(modifier=models.Modifier.IDF)},
        shard_number=1,
        hnsw_config=models.HnswConfigDiff(m=HNSW_M, ef_construct=HNSW_EF_CONSTRUCT),
        optimizers_config=optimizer_config(False),
    )
    for field in KEYWORD_INDEXES:
        client.create_payload_index(name, field, models.PayloadSchemaType.KEYWORD)
    for field in FLOAT_INDEXES:
        client.create_payload_index(name, field, models.PayloadSchemaType.FLOAT)
    client.create_payload_index(name, GEO_INDEX, models.PayloadSchemaType.GEO)
    return True
