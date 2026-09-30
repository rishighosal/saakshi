"""On-device semantic memory built on Qdrant Edge.

Layout follows Qdrant's edge synchronisation guide, extended to regions:

  data/<device>/memory/local            mutable EdgeShard: everything captured on this device
  data/<device>/memory/mirror/<region>  read-only copies of server shards, one per region
                                        (project) this device serves; "_all" when the server
                                        uses the global layout

Mirrors are created from a shard snapshot and refreshed with partial snapshots,
so only changed segments travel. Every query runs on the local shard and every
mirror and results are merged by id, the device's own copy first.

Search is built from Qdrant's own query primitives, so each step can be shown
to the user as a query plan:
  dense CLIP prefetch + sparse BM25 prefetch -> RRF fusion
  -> optional Formula re-scoring (recency decay, distance decay)
  -> optional MMR diversification
  with payload filters (project, kind, HQ verdict, geo radius).
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from qdrant_edge import (
    CountRequest,
    DecayKind,
    Direction,
    Distance,
    EdgeConfig,
    EdgeShard,
    EdgeSparseVectorParams,
    EdgeVectorParams,
    Expression,
    FacetRequest,
    FieldCondition,
    Filter,
    Formula,
    Fusion,
    GeoPoint,
    GeoRadius,
    HnswIndexConfig,
    MatchValue,
    Mmr,
    Modifier,
    OrderBy,
    PayloadSchemaType,
    Point,
    Prefetch,
    Query,
    QueryRequest,
    RecommendQuery,
    ScalarQuantizationConfig,
    ScalarType,
    ScrollRequest,
    SearchParams,
    SparseVector,
    UpdateOperation,
)

from saakshi_core.schema import (
    CLIP_DIM,
    FLOAT_INDEXES,
    GEO_INDEX,
    KEYWORD_INDEXES,
    VECTOR_BM25,
    VECTOR_CLIP,
)

from .diskspace import IS_WINDOWS, file_allocated_bytes, release_unused_space, shard_path_problem

log =logging.getLogger("saakshi.field.memory")

GLOBAL_KEY = "_all"
# HNSW search breadth. Measured on 50k CLIP-like vectors: ef=512 gives recall@10 = 0.98
# at ~1.3 ms (default ef: 0.66 at ~0.4 ms). See docs/BENCHMARKS.md.
HNSW_EF = 512


# A denser HNSW graph than the default (m=16, ef_construct=100). On clustered
# photo embeddings the default reaches recall@10 of ~0.85 at 100k points; this
# reaches ~0.98 at ef=512 for ~1.5x build time (bench/bench_search.py).
HNSW_M = 32
HNSW_EF_CONSTRUCT = 256


def hnsw_config() -> HnswIndexConfig:
    return HnswIndexConfig(HNSW_M, HNSW_EF_CONSTRUCT, 10000)


def edge_config(quantized: bool = False) -> EdgeConfig:
    """Vector layout identical to the server collection.

    With `quantized`, dense vectors are also kept as int8 (4x smaller in RAM);
    search uses the int8 copy and rescores with the originals.
    """
    q = ScalarQuantizationConfig(type=ScalarType.Int8, quantile=0.99, always_ram=True) if quantized else None
    return EdgeConfig(
        vectors={VECTOR_CLIP: EdgeVectorParams(size=CLIP_DIM, distance=Distance.Cosine, quantization_config=q, hnsw_config=hnsw_config())},
        sparse_vectors={VECTOR_BM25: EdgeSparseVectorParams(modifier=Modifier.Idf)},
    )


def _index_shard(shard: EdgeShard) -> None:
    for f in KEYWORD_INDEXES:
        shard.update(UpdateOperation.create_field_index(f, PayloadSchemaType.Keyword))
    for f in FLOAT_INDEXES:
        shard.update(UpdateOperation.create_field_index(f, PayloadSchemaType.Float))
    shard.update(UpdateOperation.create_field_index(GEO_INDEX, PayloadSchemaType.Geo))


def _dir_bytes(p: Path) -> int:
    """Bytes actually allocated on disk (Qdrant preallocates sparse files, so st_size overstates)."""
    if not p.exists():
        return 0
    return sum(file_allocated_bytes(f) for f in p.rglob("*") if f.is_file())


@dataclass
class Hit:
    id: str
    score: float
    payload: Dict[str, Any]
    source: str          # "local" or "mirror"
    region: str = ""     # mirror key for mirror hits


@dataclass
class SearchSpec:
    """What the user asked for, in Qdrant terms."""

    sparse: Optional[Tuple[List[int], List[float]]] = None
    dense: Optional[List[float]] = None
    limit: int = 24
    project_id: Optional[str] = None
    kind: Optional[str] = "evidence"
    recency_hours: Optional[float] = None    # half-life for the recency boost
    near: Optional[Tuple[float, float]] = None  # (lat, lon)
    radius_m: Optional[float] = None         # hard geo filter around `near`
    boost_near: bool = False                 # soft distance boost around `near`
    diverse: bool = False                    # MMR over dense candidates
    verified_only: bool = False              # HQ verdict == verified
    status: Optional[str] = None             # site status claim
    source: str = "all"                      # all | local | mirror
    candidates: int = 60
    hnsw_ef: int = HNSW_EF


@dataclass
class SearchResult:
    hits: List[Hit]
    ms: float
    plan: List[str] = field(default_factory=list)
    shards: int = 0


class DeviceMemory:
    def __init__(self, root: Path, quantized: bool = False):
        self.root = Path(root)
        self.local_path = self.root / "local"
        self.mirror_root = self.root / "mirror"
        self.delta_root = self.root / "delta"
        self.quantized = quantized
        self._lock = threading.RLock()
        self._flush_timer: Optional[threading.Timer] = None
        self._bytes_cache: Dict[Path, Tuple[float, int]] = {}
        self.path_problem = shard_path_problem(self.mirror_root)
        if self.path_problem:
            log.warning(self.path_problem)
        self._closed = False
        self.local = self._open_local()
        self.mirrors: Dict[str, EdgeShard] = self._open_mirrors()
        # Small per-region shards holding points fetched by delta pulls since the
        # last snapshot. Searched before the mirror (newer wins), emptied when a
        # snapshot brings the mirror up to date.
        self.deltas: Dict[str, EdgeShard] = self._open_dir(self.delta_root)

    # ---------------------------------------------------------------- lifecycle
    def _open_local(self) -> EdgeShard:
        self.local_path.mkdir(parents=True, exist_ok=True)
        if any(self.local_path.iterdir()):
            release_unused_space(self.local_path)
            return EdgeShard.load(str(self.local_path))
        shard = EdgeShard.create(str(self.local_path), edge_config(self.quantized))
        _index_shard(shard)
        return shard

    def _open_mirrors(self) -> Dict[str, EdgeShard]:
        if self.mirror_root.exists() and any(p.name in ("wal", "segments") for p in self.mirror_root.iterdir()):
            # Older single-mirror layout: it is only a cache of the server, rebuild it
            shutil.rmtree(self.mirror_root, ignore_errors=True)
        return self._open_dir(self.mirror_root)

    @staticmethod
    def _open_dir(root: Path) -> Dict[str, EdgeShard]:
        shards: Dict[str, EdgeShard] = {}
        root.mkdir(parents=True, exist_ok=True)
        for d in sorted(p for p in root.iterdir() if p.is_dir()):
            if not any(d.iterdir()):
                continue
            try:
                release_unused_space(d)
                shards[d.name] = EdgeShard.load(str(d))
            except Exception as exc:
                log.warning("Shard %s unreadable, will re-download: %s", d, exc)
                shutil.rmtree(d, ignore_errors=True)
        return shards

    DISK_BYTES_TTL_S = 30.0

    def _disk_bytes(self, path: Path) -> int:
        """Disk use of a shard, cached briefly: walking ~1,000 shard files takes ~0.2 s and the UI polls status."""
        now = time.monotonic()
        hit = self._bytes_cache.get(path)
        if hit is None or now - hit[0] > self.DISK_BYTES_TTL_S:
            hit = (now, _dir_bytes(path))
            self._bytes_cache[path] = hit
        return hit[1]

    def close(self) -> None:
        with self._lock:
            self.flush()
            self._closed = True
            for shard in [self.local, *self.deltas.values(), *self.mirrors.values()]:
                try:
                    shard.close()
                except Exception as exc:  # closing the rest matters more than one failure
                    log.warning("Could not close a shard cleanly: %s", exc)

    def _shards(self, source: str = "all") -> Iterable[Tuple[str, str, EdgeShard]]:
        if source in ("all", "local"):
            yield "local", "", self.local
        if source in ("all", "mirror"):
            for key, shard in list(self.deltas.items()):
                yield "mirror", key, shard
            for key, shard in list(self.mirrors.items()):
                yield "mirror", key, shard

    # ----------------------------------------------------------- mirror updates
    def mirror_keys(self) -> List[str]:
        return sorted(self.mirrors)

    def replace_mirror_from_snapshot(self, key: str, snapshot_file: Path) -> None:
        """First pull of a region: unpack a full shard snapshot into its mirror."""
        with self._lock:
            old = self.mirrors.pop(key, None)
            if old is not None:
                old.close()
            path = self.mirror_root / key
            shutil.rmtree(path, ignore_errors=True)
            EdgeShard.unpack_snapshot(str(snapshot_file), str(path))
            release_unused_space(path)
            self.mirrors[key] = EdgeShard.load(str(path))
            self.clear_delta(key)

    def apply_partial_snapshot(self, key: str, snapshot_file: Path, tmp_dir: Optional[Path] = None) -> None:
        with self._lock:
            shard = self.mirrors.get(key)
            if shard is None:
                raise RuntimeError(f"No mirror for {key}; pull a full snapshot first")
            shard.update_from_snapshot(str(snapshot_file), str(tmp_dir) if tmp_dir else None)
            if IS_WINDOWS:  # the new segments are preallocated in full on NTFS (see diskspace.py)
                shard.close()
                release_unused_space(self.mirror_root / key)
                self.mirrors[key] = EdgeShard.load(str(self.mirror_root / key))
            self.clear_delta(key)

    def mirror_manifest(self, key: str) -> Optional[Any]:
        with self._lock:
            shard = self.mirrors.get(key)
            return shard.snapshot_manifest() if shard is not None else None

    def drop_mirror(self, key: str) -> None:
        with self._lock:
            shard = self.mirrors.pop(key, None)
            if shard is not None:
                shard.close()
            shutil.rmtree(self.mirror_root / key, ignore_errors=True)
            self.clear_delta(key)

    # -------------------------------------------------------------- delta pulls
    def delta_upsert(self, key: str, points: List[Point]) -> None:
        """Points changed on the server since the mirror's snapshot (see SyncEngine._pull_delta)."""
        if not points:
            return
        with self._lock:
            shard = self.deltas.get(key)
            if shard is None:
                path = self.delta_root / key
                shutil.rmtree(path, ignore_errors=True)
                path.mkdir(parents=True, exist_ok=True)
                shard = EdgeShard.create(str(path), edge_config())
                _index_shard(shard)
                self.deltas[key] = shard
            shard.update(UpdateOperation.upsert_points(points))
            shard.flush()

    def delta_count(self, key: str) -> int:
        with self._lock:
            shard = self.deltas.get(key)
            return int(shard.info().points_count) if shard is not None else 0

    def clear_delta(self, key: str) -> None:
        """The mirror caught up (snapshot applied): the delta points are in it now."""
        with self._lock:
            shard = self.deltas.pop(key, None)
            if shard is not None:
                shard.close()
            shutil.rmtree(self.delta_root / key, ignore_errors=True)

    def colleague_points(self, device_id: str) -> int:
        """Distinct points in the mirrors (and deltas) captured by other devices. Cached until a shard's size changes."""
        with self._lock:
            shards = [(k, sh) for k, sh in self.mirrors.items()] + [("delta:" + k, sh) for k, sh in self.deltas.items()]
            sig = tuple(sorted((k, int(sh.info().points_count)) for k, sh in shards))
        cached = getattr(self, "_colleagues_cache", None)
        if cached and cached[0] == (sig, device_id):
            return cached[1]
        n = sum(1 for _pid, p, _ in self.scroll_all("mirror") if p.get("device_id") != device_id and p.get("kind", "evidence") == "evidence")
        self._colleagues_cache = ((sig, device_id), n)
        return n

    def newest_update(self, key: str) -> float:
        """Largest updated_ts held for a region (mirror or delta), via the float index."""
        best = 0.0
        with self._lock:
            for shard in (self.deltas.get(key), self.mirrors.get(key)):
                if shard is None:
                    continue
                try:
                    recs, _ = shard.scroll(ScrollRequest(limit=1, order_by=OrderBy("updated_ts", Direction.Desc), with_payload=True))
                except Exception as exc:  # e.g. a mirror without the float index: treat as empty
                    log.debug("Newest-update lookup failed for %s: %s", key, exc)
                    continue
                if recs:
                    best = max(best, float((recs[0].payload or {}).get("updated_ts") or 0.0))
        return best

    def drop_all_mirrors(self) -> None:
        for key in list(self.mirrors):
            self.drop_mirror(key)

    def ensure_scroll_mirror(self, key: str) -> None:
        """Mirror for servers that refuse snapshot downloads: an empty shard we fill by scrolling."""
        with self._lock:
            if key in self.mirrors:
                return
            path = self.mirror_root / key
            shutil.rmtree(path, ignore_errors=True)
            path.mkdir(parents=True, exist_ok=True)
            shard = EdgeShard.create(str(path), edge_config())
            _index_shard(shard)
            self.mirrors[key] = shard

    def mirror_upsert(self, key: str, points: List[Point]) -> None:
        if not points:
            return
        with self._lock:
            shard = self.mirrors.get(key)
            if shard is None:
                raise RuntimeError(f"mirror {key} missing")
            shard.update(UpdateOperation.upsert_points(points))
            shard.flush()

    # ------------------------------------------------------------------ writes
    # Qdrant Edge keeps recent writes in its WAL until flush(); on Windows a process
    # that exits without close() (crash, power cut, killed) loses all of them. A new
    # capture is flushed at once (~30 ms); payload updates, which the sync loop makes
    # in bursts, are flushed together shortly after.
    FLUSH_DELAY_S = 1.0

    def upsert(self, point_id: str, clip: List[float], sparse: Tuple[List[int], List[float]], payload: Dict[str, Any]) -> None:
        vector: Dict[str, Any] = {VECTOR_CLIP: list(clip)}
        if sparse and sparse[0]:
            vector[VECTOR_BM25] = SparseVector(list(sparse[0]), list(sparse[1]))
        with self._lock:
            self.local.update(UpdateOperation.upsert_points([Point(point_id, vector, payload)]))
            self.local.flush()

    def set_payload(self, point_id: str, fields: Dict[str, Any]) -> None:
        with self._lock:
            self.local.update(UpdateOperation.set_payload([point_id], fields))
            self._flush_soon()

    def delete(self, point_id: str) -> None:
        with self._lock:
            self.local.update(UpdateOperation.delete_points([point_id]))
            self._flush_soon()

    def _flush_soon(self) -> None:
        if self._flush_timer is None:
            self._flush_timer = threading.Timer(self.FLUSH_DELAY_S, self.flush)
            self._flush_timer.daemon = True
            self._flush_timer.start()

    def flush(self) -> None:
        with self._lock:
            if self._flush_timer is not None:
                self._flush_timer.cancel()
                self._flush_timer = None
            if not self._closed:
                self.local.flush()

    def optimize(self) -> Dict[str, Any]:
        """Build the HNSW index and merge segments (Qdrant's in-process optimizer).

        Without it, search is exact brute force; with it, a 50k-point shard answers
        in well under a millisecond. Runs in the background when the device is idle.
        """
        t0 = time.perf_counter()
        with self._lock:
            changed = self.local.optimize()
            info = self.local.info()
        return {"changed": bool(changed), "seconds": round(time.perf_counter() - t0, 3),
                "indexed_vectors": int(info.indexed_vectors_count), "points": int(info.points_count), "segments": int(info.segments_count)}

    # ------------------------------------------------------------------- reads
    def get(self, point_id: str, with_vector: bool = False) -> Optional[Tuple[Dict[str, Any], Optional[Dict[str, Any]], str]]:
        """Return (payload, vectors, source) from local first, then mirrors."""
        with self._lock:
            for source, _key, shard in self._shards():
                recs = shard.retrieve([point_id], True, with_vector)
                if recs:
                    r = recs[0]
                    return dict(r.payload or {}), (r.vector if with_vector else None), source
        return None

    def find(self, shard: str = "local", **equals: Any) -> List[Tuple[str, Dict[str, Any], Any]]:
        """Points whose payload fields equal the given values (filtered inside Qdrant Edge)."""
        return self.scroll_all(shard, flt=_filter(**equals))

    def scroll_all(self, shard: str = "local", flt: Optional[Filter] = None, with_vector: bool = False, page: int = 256) -> List[Tuple[str, Dict[str, Any], Any]]:
        """Scroll one shard ("local"), every mirror ("mirror"), or one mirror by key."""
        if shard == "local":
            targets = [self.local]
        elif shard == "mirror":
            targets = list(self.deltas.values()) + list(self.mirrors.values())
        else:
            targets = [m[shard] for m in (self.deltas, self.mirrors) if shard in m]
        out: List[Tuple[str, Dict[str, Any], Any]] = []
        seen = set()
        with self._lock:
            for target in targets:
                offset = None
                while True:
                    recs, offset = target.scroll(ScrollRequest(offset=offset, limit=page, filter=flt, with_payload=True, with_vector=with_vector))
                    for r in recs:
                        rid = str(r.id)
                        if rid in seen:
                            continue
                        seen.add(rid)
                        out.append((rid, dict(r.payload or {}), r.vector if with_vector else None))
                    if offset is None:
                        break
        return out

    def count(self, shard: str = "local", **equals: Any) -> int:
        target = self.local if shard == "local" else self.mirrors.get(shard)
        if target is None:
            return 0
        with self._lock:
            return int(target.count(CountRequest(exact=True, filter=_filter(**equals))))

    def facet(self, key: str, shard: str = "local", limit: int = 20) -> List[Tuple[Any, int]]:
        target = self.local if shard == "local" else self.mirrors.get(shard)
        if target is None:
            return []
        with self._lock:
            try:
                return [(h.value, h.count) for h in target.facet(FacetRequest(key=key, limit=limit, exact=True))]
            except Exception:
                return []

    def _query(self, shard: EdgeShard, req: QueryRequest) -> List[Any]:
        try:
            return list(shard.query(req))
        except Exception as exc:  # an empty shard or missing sparse data should not break search
            log.debug("query failed on shard: %s", exc)
            return []

    # ------------------------------------------------------------------ search
    def search(self, spec: SearchSpec) -> SearchResult:
        conds = [FieldCondition(k, match=MatchValue(v)) for k, v in (("project_id", spec.project_id), ("kind", spec.kind), ("status_claim", spec.status)) if v]
        plan: List[str] = []
        if spec.verified_only:
            conds.append(FieldCondition("cloud.integrity_level", match=MatchValue("verified")))
            plan.append("Filter: HQ verdict is verified (nested payload field cloud.integrity_level)")
        if spec.near and spec.radius_m and not spec.boost_near:
            conds.append(FieldCondition(GEO_INDEX, geo_radius=GeoRadius(GeoPoint(lon=spec.near[1], lat=spec.near[0]), float(spec.radius_m))))
            plan.append(f"Filter: within {spec.radius_m / 1000:.1f} km (geo index on loc)")
        if spec.project_id:
            plan.append(f"Filter: project {spec.project_id} (keyword index)")
        flt = Filter(must=conds) if conds else None
        n = max(spec.limit * 3, spec.candidates)

        dense_q = Query.Nearest(list(spec.dense), using=VECTOR_CLIP) if spec.dense is not None else None
        dense_params = SearchParams(hnsw_ef=spec.hnsw_ef)
        sparse_q = Query.Nearest(SparseVector(list(spec.sparse[0]), list(spec.sparse[1])), using=VECTOR_BM25) if spec.sparse and spec.sparse[0] else None
        if dense_q is None and sparse_q is None:
            return SearchResult([], 0.0, ["Nothing to search for"], 0)

        # Stage 1: candidates
        if dense_q is not None and sparse_q is not None:
            candidates = Prefetch(limit=n, prefetches=[Prefetch(limit=n, query=dense_q, filter=flt, params=dense_params),
                                                       Prefetch(limit=n, query=sparse_q, filter=flt)],
                                  query=Fusion.Rrf(k=60), filter=flt)
            plan.insert(0, f"Dense CLIP search ({n} candidates) + sparse BM25 search ({n}) fused with Reciprocal Rank Fusion")
        elif dense_q is not None:
            candidates = Prefetch(limit=n, query=dense_q, filter=flt, params=dense_params)
            plan.insert(0, f"Dense CLIP search ({n} candidates, HNSW ef={spec.hnsw_ef})")
        else:
            candidates = Prefetch(limit=n, query=sparse_q, filter=flt)
            plan.insert(0, f"Keyword BM25 search ({n} candidates; semantic model not loaded)")

        # Stage 2: re-scoring or diversification
        boosts = []
        now = time.time()
        if spec.recency_hours:
            boosts.append(Expression.Decay(DecayKind.Exp, Expression.Variable("captured_ts"), Expression.Constant(now),
                                           scale=float(spec.recency_hours) * 3600.0, midpoint=0.5))
            plan.append(f"Re-score: recency decay, half weight after {spec.recency_hours:g} h (Formula + exp decay)")
        if spec.near and spec.boost_near:
            boosts.append(Expression.Decay(DecayKind.Gauss, Expression.GeoDistance(GeoPoint(lon=spec.near[1], lat=spec.near[0]), GEO_INDEX),
                                           Expression.Constant(0.0), scale=float(spec.radius_m or 2000.0), midpoint=0.5))
            plan.append("Re-score: closer evidence first (Formula + Gaussian distance decay)")
        if spec.diverse and spec.dense is not None:
            query = Mmr(list(spec.dense), 0.5, n, using=VECTOR_CLIP)
            req = QueryRequest(limit=spec.limit, prefetches=[candidates], query=query, filter=flt, with_payload=True)
            plan.append("Diversify: Maximal Marginal Relevance (λ = 0.5) so near-identical shots do not crowd the list")
        elif boosts:
            boost = boosts[0] if len(boosts) == 1 else Expression.Mult(boosts)
            formula = Formula(Expression.Mult([Expression.Variable("$score"), Expression.Sum([Expression.Constant(0.5), Expression.Mult([Expression.Constant(0.5), boost])])]),
                              defaults={"captured_ts": 0.0, GEO_INDEX: {"lat": 0.0, "lon": 0.0}})
            req = QueryRequest(limit=spec.limit, prefetches=[candidates], query=formula, with_payload=True)
        else:
            if dense_q is not None and sparse_q is not None:
                req = QueryRequest(limit=spec.limit, prefetches=candidates.prefetches, query=Fusion.Rrf(k=60), filter=flt, with_payload=True)
            else:
                req = QueryRequest(limit=spec.limit, query=dense_q or sparse_q, filter=flt, with_payload=True,
                                   params=dense_params if dense_q is not None else None)
        if spec.diverse and spec.dense is None:
            plan.append("Diversity needs the semantic model; skipped")

        t0 = time.perf_counter()
        merged: Dict[str, Hit] = {}
        shards = 0
        with self._lock:
            for source, key, shard in self._shards(spec.source):
                shards += 1
                for r in self._query(shard, req):
                    pid = str(r.id)
                    if pid not in merged:
                        merged[pid] = Hit(pid, float(r.score), dict(r.payload or {}), source, key)
        ms = (time.perf_counter() - t0) * 1000
        parts = []
        if spec.source in ("all", "local"):
            parts.append("local")
        if spec.source in ("all", "mirror"):
            if self.mirrors:
                parts.append(f"{len(self.mirrors)} region mirror(s)")
            if self.deltas:
                parts.append(f"{len(self.deltas)} delta shard(s)")
        plan.append(f"Ran on {shards} shard(s) on this device ({' + '.join(parts) or 'none'}), merged by id")
        hits = sorted(merged.values(), key=lambda h: h.score, reverse=True)[: spec.limit]
        return SearchResult(hits, ms, plan, shards)

    def hybrid_search(self, text_sparse, dense, limit: int = 20, project_id: Optional[str] = None, kind: Optional[str] = "evidence"):
        """Plain hybrid search (kept for callers that do not need the options)."""
        res = self.search(SearchSpec(sparse=text_sparse, dense=dense, limit=limit, project_id=project_id, kind=kind))
        return res.hits, res.ms

    def recommend(self, positive: List[List[float]], negative: List[List[float]], limit: int = 12) -> List[Hit]:
        """'More like these, less like those' using Qdrant's recommend query."""
        req = QueryRequest(limit=limit, query=Query.RecommendBestScore(RecommendQuery(positive, negative), using=VECTOR_CLIP),
                           filter=_filter(kind="evidence"), with_payload=True)
        out: Dict[str, Hit] = {}
        with self._lock:
            for source, key, shard in self._shards():
                for r in self._query(shard, req):
                    pid = str(r.id)
                    if pid not in out:
                        out[pid] = Hit(pid, float(r.score), dict(r.payload or {}), source, key)
        return sorted(out.values(), key=lambda h: h.score, reverse=True)[:limit]

    def nearest_images(self, dense: List[float], limit: int = 5, exclude_id: Optional[str] = None) -> List[Hit]:
        """Visual neighbours across all shards (used for duplicate detection)."""
        req = QueryRequest(limit=limit + 1, query=Query.Nearest(list(dense), using=VECTOR_CLIP), with_payload=True, filter=_filter(kind="evidence"))
        out: Dict[str, Hit] = {}
        with self._lock:
            for source, key, shard in self._shards():
                for r in self._query(shard, req):
                    pid = str(r.id)
                    if pid == exclude_id or pid in out:
                        continue
                    out[pid] = Hit(pid, float(r.score), dict(r.payload or {}), source, key)
        return sorted(out.values(), key=lambda h: h.score, reverse=True)[:limit]

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            local_info = self.local.info()
            mirrors = {k: int(s.info().points_count) for k, s in self.mirrors.items()}
            deltas = {k: int(s.info().points_count) for k, s in self.deltas.items()}
        return {
            "local_points": int(local_info.points_count),
            "local_segments": int(local_info.segments_count),
            "local_indexed": int(local_info.indexed_vectors_count),
            "mirror_points": sum(mirrors.values()),
            "mirrors": mirrors,
            "delta_points": deltas,
            "mirror_ready": bool(self.mirrors),
            "path_problem": self.path_problem,
            "local_bytes": self._disk_bytes(self.local_path),
            "mirror_bytes": {k: self._disk_bytes(self.mirror_root / k) for k in mirrors},
            "quantized": self.quantized,
        }


def _filter(**equals: Any) -> Optional[Filter]:
    conds = [FieldCondition(k, match=MatchValue(v)) for k, v in equals.items() if v not in (None, "")]
    return Filter(must=conds) if conds else None
