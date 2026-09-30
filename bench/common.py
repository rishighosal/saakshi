"""Shared helpers for the benchmark suite.

Embeddings are synthetic but shaped like CLIP image embeddings: unit vectors
drawn around a few hundred cluster centres (photos of similar scenes), so that
nearest-neighbour search and quantization behave realistically. Field notes are
generated from templates over the NGO taxonomy so BM25 has real words to match.
"""

from __future__ import annotations

import json
import os
import platform
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "benchmarks"

SITES = ["Park Street corner", "Gariahat drain", "Pond A", "Embankment North", "Creek Mouth", "School lane", "Market road", "Canal bank"]
THINGS = ["garbage pile", "plastic waste", "saplings planted", "mangrove saplings", "flooded road", "clean street", "broken toilet",
          "new handwash station", "eroded embankment", "silt removed", "water hyacinth", "solar panel installed"]
STATES = ["before clean-up", "after clean-up", "needs attention", "completed", "in progress", "damaged after rain"]


def embeddings(n: int, dim: int = 512, clusters: int = 300, spread: float = 0.35, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    centers = rng.normal(size=(clusters, dim)).astype(np.float32)
    x = centers[rng.integers(0, clusters, n)] + spread * rng.normal(size=(n, dim)).astype(np.float32)
    x /= np.linalg.norm(x, axis=1, keepdims=True)
    return x


def queries_near(x: np.ndarray, k: int, noise: float = 0.15, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    q = x[rng.integers(0, len(x), k)] + noise * rng.normal(size=(k, x.shape[1])).astype(np.float32)
    q /= np.linalg.norm(q, axis=1, keepdims=True)
    return q


def notes(n: int, seed: int = 2) -> List[str]:
    rng = np.random.default_rng(seed)
    return [f"{THINGS[rng.integers(len(THINGS))]} at {SITES[rng.integers(len(SITES))]}, {STATES[rng.integers(len(STATES))]}" for _ in range(n)]


def exact_topk(x: np.ndarray, q: np.ndarray, k: int = 10) -> List[set]:
    out = []
    for v in q:
        sims = x @ v
        idx = np.argpartition(-sims, k)[:k]
        out.append({int(i) for i in idx})
    return out


def pct(values: List[float], p: float) -> float:
    return float(np.percentile(np.asarray(values), p)) if values else float("nan")


def timed(fn, *args, **kwargs) -> Tuple[Any, float]:
    t0 = time.perf_counter()
    r = fn(*args, **kwargs)
    return r, (time.perf_counter() - t0) * 1000


def dir_bytes(p: Path) -> int:
    """Bytes a closed shard occupies on disk, as the device keeps it.

    Qdrant preallocates its files. Linux keeps the unused part sparse; on Windows the
    app releases it when it opens a shard (field/app/diskspace.py), so that is done
    here too before measuring. st_size would overstate either way."""
    from field.app.diskspace import release_unused_space
    from field.app.memory import _dir_bytes

    release_unused_space(Path(p))
    return _dir_bytes(Path(p))


def machine() -> Dict[str, Any]:
    return {"python": platform.python_version(), "platform": platform.platform(), "processor": platform.processor() or platform.machine(),
            "cpus": os.cpu_count(), "date": time.strftime("%Y-%m-%d %H:%M")}


def save(name: str, data: Dict[str, Any]) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / f"{name}.json"
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return p
