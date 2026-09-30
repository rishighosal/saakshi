"""Search on the device vs. the alternatives.

For each memory size N it builds the same data in:
  * Qdrant Edge, in process, after optimize() (HNSW index)          <- what the device uses
  * Qdrant Edge with int8 scalar quantization                         <- smaller RAM footprint
  * Qdrant Edge before optimize() (exact scan)                        <- what you get without indexing
  * NumPy brute force over a float32 matrix                           <- "just keep vectors in an array"
  * Qdrant server over HTTP on localhost (optional, QDRANT_URL)       <- cloud search, best case: no network
and measures p50/p95 latency of top-10 queries and recall@10 against exact search.
Hybrid (dense + BM25 with RRF) latency is measured on Qdrant Edge too.

    python -m bench.bench_search --sizes 1000 10000 50000
"""

from __future__ import annotations

import argparse
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
from qdrant_edge import (
    Bm25,
    Distance,
    EdgeConfig,
    EdgeShard,
    EdgeSparseVectorParams,
    EdgeVectorParams,
    Fusion,
    HnswIndexConfig,
    Modifier,
    Point,
    Prefetch,
    Query,
    QueryRequest,
    ScalarQuantizationConfig,
    ScalarType,
    SearchParams,
    UpdateOperation,
)

from bench.common import dir_bytes, embeddings, exact_topk, machine, notes, pct, queries_near, save
from field.app.memory import HNSW_EF_CONSTRUCT, HNSW_M

DIM = 512


def build_edge(path: Path, x: np.ndarray, texts: List[str], quantized: bool = False, optimize: bool = True, tuned: bool = True) -> EdgeShard:
    q = ScalarQuantizationConfig(type=ScalarType.Int8, quantile=0.99, always_ram=True) if quantized else None
    hnsw = HnswIndexConfig(HNSW_M, HNSW_EF_CONSTRUCT, 10000) if tuned else None
    cfg = EdgeConfig(vectors={"clip": EdgeVectorParams(size=DIM, distance=Distance.Cosine, quantization_config=q, hnsw_config=hnsw)},
                     sparse_vectors={"bm25": EdgeSparseVectorParams(modifier=Modifier.Idf)})
    path.mkdir(parents=True, exist_ok=True)
    s = EdgeShard.create(str(path), cfg)
    bm = Bm25()
    B = 1000
    for i in range(0, len(x), B):
        pts = [Point(i + j, {"clip": x[i + j].tolist(), "bm25": bm.embed_document(texts[i + j])}, {"note": texts[i + j]})
               for j in range(min(B, len(x) - i))]
        s.update(UpdateOperation.upsert_points(pts))
    if optimize:
        s.optimize()
    return s


def run_edge(s: EdgeShard, qs: np.ndarray, truth: List[set], hybrid_texts: List[str], ef: int = 512) -> Dict[str, Any]:
    params = SearchParams(hnsw_ef=ef) if ef else None
    lat, hits = [], 0
    for v, t in zip(qs, truth):
        t0 = time.perf_counter()
        res = s.query(QueryRequest(limit=10, query=Query.Nearest(v.tolist(), using="clip"), params=params))
        lat.append((time.perf_counter() - t0) * 1000)
        hits += len({int(r.id) for r in res} & t)
    bm = Bm25()
    hlat = []
    for v, text in zip(qs, hybrid_texts):
        req = QueryRequest(limit=10, prefetches=[Prefetch(limit=60, query=Query.Nearest(v.tolist(), using="clip"), params=params),
                                                 Prefetch(limit=60, query=Query.Nearest(bm.embed_query(text), using="bm25"))],
                           query=Fusion.Rrf(k=60))
        t0 = time.perf_counter()
        s.query(req)
        hlat.append((time.perf_counter() - t0) * 1000)
    return {"p50_ms": pct(lat, 50), "p95_ms": pct(lat, 95), "recall_at_10": hits / (10 * len(qs)),
            "hybrid_p50_ms": pct(hlat, 50), "hybrid_p95_ms": pct(hlat, 95)}


def run_numpy(x: np.ndarray, qs: np.ndarray) -> Dict[str, Any]:
    lat = []
    for v in qs:
        t0 = time.perf_counter()
        sims = x @ v
        idx = np.argpartition(-sims, 10)[:10]
        idx[np.argsort(-sims[idx])]
        lat.append((time.perf_counter() - t0) * 1000)
    return {"p50_ms": pct(lat, 50), "p95_ms": pct(lat, 95), "recall_at_10": 1.0}


def ef_sweep(s: EdgeShard, qs: np.ndarray, truth: List[set]) -> List[Dict[str, Any]]:
    out = []
    for ef in (64, 128, 256, 512, 1024):
        lat, hits = [], 0
        for v, t in zip(qs, truth):
            t0 = time.perf_counter()
            res = s.query(QueryRequest(limit=10, query=Query.Nearest(v.tolist(), using="clip"), params=SearchParams(hnsw_ef=ef)))
            lat.append((time.perf_counter() - t0) * 1000)
            hits += len({int(r.id) for r in res} & t)
        out.append({"ef": ef, "p50_ms": pct(lat, 50), "p95_ms": pct(lat, 95), "recall_at_10": hits / (10 * len(qs))})
    return out


def run_server(url: str, api_key: str, x: np.ndarray, qs: np.ndarray, truth: List[set], ef: int = 512) -> Dict[str, Any]:
    from qdrant_client import QdrantClient, models

    cli = QdrantClient(url=url, api_key=api_key or None, timeout=120)
    name = f"bench_{uuid.uuid4().hex[:8]}"
    cli.create_collection(name, vectors_config={"clip": models.VectorParams(size=DIM, distance=models.Distance.COSINE)},
                          hnsw_config=models.HnswConfigDiff(m=HNSW_M, ef_construct=HNSW_EF_CONSTRUCT))
    try:
        B = 1000
        for i in range(0, len(x), B):
            cli.upsert(name, [models.PointStruct(id=i + j, vector={"clip": x[i + j].tolist()}) for j in range(min(B, len(x) - i))], wait=True)
        # let the server finish indexing
        for _ in range(120):
            info = cli.get_collection(name)
            if info.status == models.CollectionStatus.GREEN and (len(x) < 5000 or (info.indexed_vectors_count or 0) >= 0.9 * len(x)):
                break
            time.sleep(0.5)
        lat, hits = [], 0
        for v, t in zip(qs, truth):
            t0 = time.perf_counter()
            res = cli.query_points(name, query=v.tolist(), using="clip", limit=10, search_params=models.SearchParams(hnsw_ef=ef))
            lat.append((time.perf_counter() - t0) * 1000)
            hits += len({int(p.id) for p in res.points} & t)
        return {"p50_ms": pct(lat, 50), "p95_ms": pct(lat, 95), "recall_at_10": hits / (10 * len(qs))}
    finally:
        cli.delete_collection(name)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", type=int, nargs="+", default=[1000, 10000, 50000])
    ap.add_argument("--queries", type=int, default=200)
    ap.add_argument("--server", default=os.environ.get("QDRANT_URL"), help="Qdrant server URL for the HTTP comparison")
    args = ap.parse_args()
    api_key = os.environ.get("QDRANT_API_KEY", "")
    results: Dict[str, Any] = {"machine": machine(), "dim": DIM, "queries": args.queries, "sizes": [],
                               "hnsw": {"m": HNSW_M, "ef_construct": HNSW_EF_CONSTRUCT, "search_ef": 512}}
    for n in args.sizes:
        print(f"\nN = {n:,}")
        x = embeddings(n)
        texts = notes(n)
        qs = queries_near(x, args.queries)
        qtexts = notes(args.queries, seed=9)
        truth = exact_topk(x, qs)
        row: Dict[str, Any] = {"n": n}
        tmp = Path(tempfile.mkdtemp(prefix="saakshi-bench-"))
        try:
            t0 = time.perf_counter()
            s = build_edge(tmp / "f32", x, texts)
            row["edge_build_s"] = round(time.perf_counter() - t0, 2)
            row["edge"] = run_edge(s, qs, truth, qtexts)
            if n == max(args.sizes):
                results["ef_sweep"] = {"n": n, "m": HNSW_M, "ef_construct": HNSW_EF_CONSTRUCT, "points": ef_sweep(s, qs, truth)}
            s.close()
            row["edge_disk_mb"] = round(dir_bytes(tmp / "f32") / 1e6, 1)
            t0 = time.perf_counter()
            s = build_edge(tmp / "dflt", x, texts, tuned=False)
            row["edge_qdrant_defaults_build_s"] = round(time.perf_counter() - t0, 2)
            row["edge_qdrant_defaults"] = run_edge(s, qs, truth, qtexts, ef=0)
            s.close()
            print("  edge (defaults)  ", {k: round(v, 3) for k, v in row["edge_qdrant_defaults"].items()})
            print("  edge (HNSW)      ", {k: round(v, 3) for k, v in row["edge"].items()})
            s = build_edge(tmp / "i8", x, texts, quantized=True)
            row["edge_int8"] = run_edge(s, qs, truth, qtexts)
            s.close()
            row["edge_int8_disk_mb"] = round(dir_bytes(tmp / "i8") / 1e6, 1)
            print("  edge (int8)      ", {k: round(v, 3) for k, v in row["edge_int8"].items()})
            s = build_edge(tmp / "raw", x, texts, optimize=False)
            row["edge_unindexed"] = run_edge(s, qs[:50], truth[:50], qtexts[:50], ef=0)
            s.close()
            print("  edge (no index)  ", {k: round(v, 3) for k, v in row["edge_unindexed"].items()})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        row["numpy"] = run_numpy(x, qs)
        row["vectors_float32_mb"] = round(x.nbytes / 1e6, 1)
        row["vectors_int8_mb"] = round(x.nbytes / 4 / 1e6, 1)
        print("  numpy brute force", {k: round(v, 3) for k, v in row["numpy"].items()})
        if args.server:
            try:
                row["server_http"] = run_server(args.server, api_key, x, qs, truth)
                row["server_url_is_local"] = any(h in args.server for h in ("127.0.0.1", "localhost"))
                print("  qdrant server    ", {k: round(v, 3) for k, v in row["server_http"].items()})
            except Exception as exc:
                print("  qdrant server     skipped:", exc)
        results["sizes"].append(row)
    path = save("search", results)
    print(f"\nSaved {path}")


if __name__ == "__main__":
    main()
