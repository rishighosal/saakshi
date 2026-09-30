"""How much data does a device download to stay in sync?

Builds a server collection with several projects (regions), then measures what
a device has to download:
  * global layout: full shard snapshot, then partial snapshot after new points
  * regional layout (custom shard keys, needs a cluster-mode server): the shard
    of ONE project, then its partial snapshot
  * no change: the 304 response
  * naive approach: downloading the full snapshot again on every pull

    python -m bench.bench_sync --global-url http://127.0.0.1:6333 --cluster-url http://127.0.0.1:6433
"""

from __future__ import annotations

import argparse
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import numpy as np
from qdrant_edge import EdgeShard

from bench.common import embeddings, machine, notes, save
from saakshi_core.embeddings import bm25_document
from saakshi_core.qdrant_server import ServerCollection, point_struct

PROJECTS = ["clean-streets", "mangrove-gosaba", "pond-revival", "school-wash", "solar-villages"]


def _points(x: np.ndarray, texts: List[str], project: str, start: int) -> List[Any]:
    out = []
    now = time.time()
    for i, (v, t) in enumerate(zip(x, texts)):
        idx, vals = bm25_document(t)
        out.append(point_struct(str(uuid.UUID(int=start + i)), v.tolist(), {"indices": idx, "values": vals},
                                {"kind": "evidence", "project_id": project, "note": t, "device_id": "bench", "captured_ts": now, "updated_ts": now,
                                 "loc": {"lat": 22.5, "lon": 88.3}}))
    return out


def wait_green(coll: ServerCollection, timeout: float = 180.0) -> float:
    """Wait until the server's optimizer has finished (collection status green)."""
    from qdrant_client import models

    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        info = coll.client.get_collection(coll.name)
        if info.status == models.CollectionStatus.GREEN and not (info.optimizer_status and getattr(info.optimizer_status, "error", None)):
            time.sleep(0.5)
            if coll.client.get_collection(coll.name).status == models.CollectionStatus.GREEN:
                return time.perf_counter() - t0
        time.sleep(0.5)
    return time.perf_counter() - t0


def _download(url: str, headers: Dict[str, str], dest: Path, manifest: Optional[Any] = None) -> Dict[str, Any]:
    t0 = time.perf_counter()
    if manifest is None:
        r = httpx.get(url, headers=headers, timeout=300.0)
    else:
        r = httpx.post(url + "/partial/create", headers=headers, json=manifest, timeout=300.0)
    ms = (time.perf_counter() - t0) * 1000
    if r.status_code == 304:
        return {"status": 304, "bytes": 0, "ms": ms}
    r.raise_for_status()
    dest.write_bytes(r.content)
    return {"status": r.status_code, "bytes": len(r.content), "ms": ms}


def _delta(coll: ServerCollection, headers: Dict[str, str], key: Optional[str], since: float) -> Dict[str, Any]:
    """What the device's delta pull downloads: a filtered scroll of points changed since `since`."""
    t0 = time.perf_counter()
    body: Dict[str, Any] = {"filter": {"must": [{"key": "updated_ts", "range": {"gt": since}}]}, "limit": 128,
                            "with_payload": True, "with_vector": True}
    if key:
        body["shard_key"] = key
    nbytes, points, offset = 0, 0, None
    while True:
        if offset is not None:
            body["offset"] = offset
        r = httpx.post(f"{coll.url}/collections/{coll.name}/points/scroll", headers=headers, json=body, timeout=60.0)
        r.raise_for_status()
        nbytes += r.num_bytes_downloaded
        res = r.json()["result"]
        points += len(res["points"])
        offset = res.get("next_page_offset")
        if offset is None:
            break
    return {"bytes": nbytes, "points": points, "ms": (time.perf_counter() - t0) * 1000,
            "bytes_per_point": nbytes / points if points else None}


def run(url: str, api_key: Optional[str], per_project: int, new_points: int, regional: bool) -> Dict[str, Any]:
    name = f"bench_sync_{uuid.uuid4().hex[:8]}"
    coll = ServerCollection(url, api_key, name, prefer_regional=regional)
    coll.ensure()
    headers = {"api-key": api_key} if api_key else {}
    tmp = Path(tempfile.mkdtemp(prefix="saakshi-syncbench-"))
    res: Dict[str, Any] = {"layout": coll.layout, "projects": len(PROJECTS), "points_per_project": per_project}
    try:
        n = per_project * len(PROJECTS)
        x = embeddings(n, seed=3)
        texts = notes(n, seed=4)
        pts: List[Any] = []
        key_of: Dict[str, str] = {}
        for pi, proj in enumerate(PROJECTS):
            chunk = _points(x[pi * per_project:(pi + 1) * per_project], texts[pi * per_project:(pi + 1) * per_project], proj, pi * per_project)
            pts += chunk
            key_of.update({str(p.id): proj for p in chunk})
        for i in range(0, len(pts), 500):
            coll.upsert(pts[i:i + 500], key_of)
        res["optimizer_settle_s"] = round(wait_green(coll), 1)
        ids = coll.shard_ids()
        target = "mangrove-gosaba" if coll.layout == "regional" else "_all"
        sid = ids[target]
        snap_url = coll.snapshot_url(sid)

        # 1. first pull
        full = _download(snap_url, headers, tmp / "full.snapshot")
        t0 = time.perf_counter()
        EdgeShard.unpack_snapshot(str(tmp / "full.snapshot"), str(tmp / "mirror"))
        shard = EdgeShard.load(str(tmp / "mirror"))
        full["load_ms"] = (time.perf_counter() - t0) * 1000
        full["points"] = int(shard.info().points_count)
        res["first_pull"] = full

        # 2. nothing changed
        res["no_change"] = _download(snap_url, headers, tmp / "noop.snapshot", shard.snapshot_manifest())

        # 2b. one new point in the device's region: delta pull vs partial snapshot
        key = target if coll.layout == "regional" else None
        proj = target if coll.layout == "regional" else PROJECTS[1]
        mark = time.time() - 0.001
        one = _points(embeddings(1, seed=21), notes(1, seed=22), proj, 20_000_000)
        coll.upsert(one, {str(p.id): proj for p in one})
        wait_green(coll)
        d1 = _delta(coll, headers, key, mark)
        p1 = _download(snap_url, headers, tmp / "one.snapshot", shard.snapshot_manifest())
        if p1["bytes"]:
            shard.update_from_snapshot(str(tmp / "one.snapshot"))
        res["one_point"] = {"delta": d1, "partial": p1}
        mark = time.time() - 0.001

        # 3. new evidence arrives: `new_points` in the device's project and the same in every other project
        extra = embeddings(new_points * len(PROJECTS), seed=11)
        etexts = notes(new_points * len(PROJECTS), seed=12)
        more, more_keys = [], {}
        for pi, proj in enumerate(PROJECTS):
            chunk = _points(extra[pi * new_points:(pi + 1) * new_points], etexts[pi * new_points:(pi + 1) * new_points], proj, 10_000_000 + pi * new_points)
            more += chunk
            more_keys.update({str(p.id): proj for p in chunk})
        coll.upsert(more, more_keys)
        wait_green(coll)
        res["incremental_delta"] = _delta(coll, headers, key, mark)
        part = _download(snap_url, headers, tmp / "partial.snapshot", shard.snapshot_manifest())
        if part["bytes"]:
            t0 = time.perf_counter()
            shard.update_from_snapshot(str(tmp / "partial.snapshot"))
            part["apply_ms"] = (time.perf_counter() - t0) * 1000
        part["points_after"] = int(shard.info().points_count)
        res["incremental_pull"] = part

        # 4. naive: download everything again
        res["naive_repull"] = _download(snap_url, headers, tmp / "again.snapshot")
        shard.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            coll.client.delete_collection(name)
        except Exception as exc:
            print(f"warning: benchmark collection {name} was not deleted: {exc}")
    return res


def server_info(url: str, api_key: Optional[str]) -> Dict[str, Any]:
    """Which server the numbers come from (no URL or key is recorded)."""
    headers = {"api-key": api_key} if api_key else {}
    info = httpx.get(url.rstrip("/") + "/", headers=headers, timeout=20).json()
    cluster = httpx.get(url.rstrip("/") + "/cluster", headers=headers, timeout=20).json().get("result", {})
    host = url.split("//")[-1].split("/")[0].split(":")[0]
    kind = "Qdrant Cloud" if host.endswith(".cloud.qdrant.io") else ("localhost" if host in ("127.0.0.1", "localhost") else "self-hosted")
    return {"kind": kind, "version": info.get("version"), "cluster_mode": cluster.get("status")}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--global-url", default=os.environ.get("QDRANT_URL", "http://127.0.0.1:6333"))
    ap.add_argument("--cluster-url", default=os.environ.get("QDRANT_CLUSTER_URL"))
    ap.add_argument("--per-project", type=int, default=2000)
    ap.add_argument("--new", type=int, default=50)
    args = ap.parse_args()
    key = os.environ.get("QDRANT_API_KEY")
    out: Dict[str, Any] = {"machine": machine(), "per_project": args.per_project, "new_points_per_project": args.new,
                           "server": {"global": server_info(args.global_url, key),
                                      **({"regional": server_info(args.cluster_url, key)} if args.cluster_url else {})}}
    print("global layout …")
    out["global"] = run(args.global_url, key, args.per_project, args.new, regional=False)
    print({k: v for k, v in out["global"].items() if isinstance(v, dict)})
    if args.cluster_url:
        print("regional layout …")
        out["regional"] = run(args.cluster_url, key, args.per_project, args.new, regional=True)
        print({k: v for k, v in out["regional"].items() if isinstance(v, dict)})
    print("Saved", save("sync", out))


if __name__ == "__main__":
    main()
