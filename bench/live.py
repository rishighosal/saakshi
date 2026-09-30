"""A quick benchmark on the running device, for the Benchmarks tab.

Takes the evidence already in this device's memory, runs the same searches on
Qdrant Edge (in process) and, if it is reachable, on the Qdrant server, and
reports the latency each one had just now on this machine and network.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

import numpy as np

from field.app.memory import SearchSpec
from saakshi_core.schema import VECTOR_CLIP


def _pct(xs: List[float], p: float) -> float:
    return round(float(np.percentile(np.asarray(xs), p)), 3) if xs else float("nan")


def _query_vectors(service, n: int) -> List[List[float]]:
    rows = service.memory.scroll_all("local", with_vector=True)[:n]
    rows += service.memory.scroll_all("mirror", with_vector=True)[: max(0, n - len(rows))]
    out = []
    for _, _, vec in rows:
        v = (vec or {}).get(VECTOR_CLIP) if isinstance(vec, dict) else None
        if v is not None:
            out.append([float(x) for x in v])
    rng = np.random.default_rng(7)
    while len(out) < n:
        v = rng.normal(size=512)
        out.append((v / np.linalg.norm(v)).tolist())
    # perturb so a query is not an exact copy of a stored point
    arr = np.asarray(out, dtype=np.float32) + 0.05 * rng.normal(size=(len(out), 512)).astype(np.float32)
    arr /= np.linalg.norm(arr, axis=1, keepdims=True)
    return arr.tolist()


def run_live(service, engine, queries: int = 30) -> Dict[str, Any]:
    t_start = time.perf_counter()
    vecs = _query_vectors(service, queries)
    stats = service.memory.stats()

    edge_ms: List[float] = []
    for v in vecs:
        t0 = time.perf_counter()
        service.memory.search(SearchSpec(sparse=None, dense=v, limit=10, kind="evidence"))
        edge_ms.append((time.perf_counter() - t0) * 1000)
    result: Dict[str, Any] = {
        "queries": len(vecs),
        "device": {"p50_ms": _pct(edge_ms, 50), "p95_ms": _pct(edge_ms, 95), "points": stats["local_points"] + stats["mirror_points"],
                   "shards": 1 + len(stats["mirrors"]) + len(stats.get("delta_points") or {}), "works_offline": True},
    }

    server: Dict[str, Any] = {"reachable": False}
    if engine is not None and service.s.qdrant_url and engine.network_mode != "offline" and engine.reachability().get("qdrant"):
        try:
            coll = engine.server
            coll.ensure()
            srv_ms: List[float] = []
            for v in vecs:
                t0 = time.perf_counter()
                coll.client.query_points(coll.name, query=v, using=VECTOR_CLIP, limit=10, with_payload=True)
                srv_ms.append((time.perf_counter() - t0) * 1000)
            server = {"reachable": True, "p50_ms": _pct(srv_ms, 50), "p95_ms": _pct(srv_ms, 95), "points": coll.count(),
                      "url": service.s.qdrant_url, "layout": coll.layout}
            if engine.network_mode == "slow":
                server["note"] = "The device is in simulated 2G mode; real 2G adds roughly 0.5-1 s per request."
        except Exception as exc:
            server = {"reachable": False, "error": str(exc)}
    else:
        server["note"] = "Server not reachable right now. On the device, search keeps working."
    result["server"] = server
    if server.get("reachable") and result["device"]["p50_ms"]:
        result["speedup_p50"] = round(server["p50_ms"] / max(result["device"]["p50_ms"], 1e-3), 1)
    status = engine.status() if engine is not None else {}
    result["sync"] = {"last_pull": status.get("last_pull"), "counters": status.get("counters")}
    result["ms"] = round((time.perf_counter() - t_start) * 1000, 1)
    return result
