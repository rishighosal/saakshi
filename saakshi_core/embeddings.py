"""Embeddings used on the device and in the cloud.

Dense: CLIP ViT-B/32 through FastEmbed (ONNX, CPU). Images and text land in
the same 512-d space, so a typed query finds photos with no captions at all.
The model files are downloaded once (~350 MB for both halves) and then work
fully offline. Run `python scripts/download_models.py` while online.

If the model cannot be loaded (first run with no internet), a colour-histogram
fallback keeps the app working: near-duplicate detection still works, text
search falls back to BM25 over notes and tags. The UI shows which mode is on.

Sparse: BM25 from `qdrant_edge.Bm25`, the same tokenizer on edge and server,
so sparse vectors pushed from a device are searchable in the cloud unchanged.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple, Union

import numpy as np
from PIL import Image

from . import taxonomy
from .imaging import color_histogram_vector, open_image
from .schema import CLIP_DIM

log = logging.getLogger("saakshi.embeddings")

CLIP_IMAGE_MODEL = "Qdrant/clip-ViT-B-32-vision"
CLIP_TEXT_MODEL = "Qdrant/clip-ViT-B-32-text"

ImageLike = Union[str, Path, bytes, Image.Image]


def default_cache_dir() -> str:
    return os.environ.get("SAAKSHI_MODEL_DIR", str(Path.home() / ".cache" / "saakshi-models"))


class Embedder:
    """Lazy CLIP embedder with a deterministic offline fallback."""

    def __init__(self, cache_dir: Optional[str] = None, use_clip: bool = True):
        self.cache_dir = cache_dir or default_cache_dir()
        self.use_clip = use_clip and os.environ.get("SAAKSHI_DISABLE_CLIP", "0") != "1"
        self._img = None
        self._txt = None
        self._lock = threading.Lock()
        self._loaded = False
        self.mode = "loading" if self.use_clip else "fallback"
        self.error: Optional[str] = None
        self._tag_vectors: Optional[np.ndarray] = None

    # ------------------------------------------------------------------ loading
    def load(self) -> str:
        """Load CLIP if possible. Safe to call many times."""
        if self._loaded:
            return self.mode
        with self._lock:
            if self._loaded:
                return self.mode
            if not self.use_clip:
                self.mode = "fallback"
                self._loaded = True
                return self.mode
            try:
                from fastembed import ImageEmbedding, TextEmbedding

                Path(self.cache_dir).mkdir(parents=True, exist_ok=True)
                self._img = ImageEmbedding(model_name=CLIP_IMAGE_MODEL, cache_dir=self.cache_dir)
                self._txt = TextEmbedding(model_name=CLIP_TEXT_MODEL, cache_dir=self.cache_dir)
                self.mode = "clip"
                log.info("CLIP models ready (cache: %s)", self.cache_dir)
            except Exception as exc:  # no internet on first run, missing onnxruntime, etc.
                self.mode = "fallback"
                self.error = f"{type(exc).__name__}: {exc}"[:300]
                log.warning("CLIP unavailable, using colour-histogram fallback: %s", self.error)
            self._loaded = True
            return self.mode

    @property
    def is_clip(self) -> bool:
        return self.load() == "clip"

    @property
    def label(self) -> str:
        mode = self.load()
        if mode == "clip":
            return "CLIP ViT-B/32 on device"
        return "Colour-histogram fallback (run scripts/download_models.py once while online)"

    # --------------------------------------------------------------- embedding
    def embed_images(self, images: Sequence[ImageLike]) -> List[List[float]]:
        if not images:
            return []
        pil = [open_image(i).convert("RGB") if not isinstance(i, Image.Image) else i.convert("RGB") for i in images]
        if self.is_clip:
            vecs = list(self._img.embed(pil))
            return [_normalise(v) for v in vecs]
        return [color_histogram_vector(p) for p in pil]

    def embed_image(self, image: ImageLike) -> List[float]:
        return self.embed_images([image])[0]

    def embed_texts(self, texts: Sequence[str]) -> List[List[float]]:
        if not texts:
            return []
        if self.is_clip:
            return [_normalise(v) for v in self._txt.embed(list(texts))]
        return [_hash_text_vector(t) for t in texts]

    def embed_text(self, text: str) -> List[float]:
        return self.embed_texts([text])[0]

    @property
    def text_matches_images(self) -> bool:
        """True when a text vector can be compared with image vectors (CLIP only)."""
        return self.is_clip

    # ------------------------------------------------------- zero-shot tagging
    def zero_shot_tags(self, image_vector: Sequence[float], top_k: int = 3, min_margin: float = 0.015) -> List[Tuple[str, float]]:
        """Offline CLIP tagging against the NGO taxonomy.

        Returns up to `top_k` (tag, score) pairs whose similarity is clearly above
        the average across all tags. Empty in fallback mode.
        """
        if not self.is_clip:
            return []
        if self._tag_vectors is None:
            prompts = [str(t["prompt"]) for t in taxonomy.TAGS]
            self._tag_vectors = np.asarray(self.embed_texts(prompts), dtype=np.float32)
        v = np.asarray(image_vector, dtype=np.float32)
        sims = self._tag_vectors @ v
        mean = float(sims.mean())
        order = np.argsort(-sims)
        out: List[Tuple[str, float]] = []
        for i in order[:top_k]:
            if sims[i] - mean >= min_margin:
                out.append((taxonomy.TAG_NAMES[int(i)], round(float(sims[i]), 4)))
        return out


def _normalise(v: Iterable[float]) -> List[float]:
    a = np.asarray(list(v), dtype=np.float32)
    n = float(np.linalg.norm(a)) or 1.0
    return (a / n).tolist()


def _hash_text_vector(text: str, dim: int = CLIP_DIM) -> List[float]:
    """Fallback text vector: hashed bag of words. Only comparable with other text vectors."""
    import zlib

    v = np.zeros(dim, dtype=np.float32)
    for tok in text.lower().split():
        tok = "".join(ch for ch in tok if ch.isalnum())
        if tok:
            v[zlib.crc32(tok.encode()) % dim] += 1.0
    n = float(np.linalg.norm(v)) or 1.0
    return (v / n).tolist()


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    va = np.asarray(a, dtype=np.float32)
    vb = np.asarray(b, dtype=np.float32)
    na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
    if na == 0 or nb == 0:
        return 0.0
    return float(va @ vb / (na * nb))


# --------------------------------------------------------------------- BM25
_bm25 = None
_bm25_lock = threading.Lock()


def bm25():
    """Shared BM25 model (qdrant_edge.Bm25). Identical tokens on edge and cloud."""
    global _bm25
    if _bm25 is None:
        with _bm25_lock:
            if _bm25 is None:
                from qdrant_edge import Bm25, Bm25Config

                _bm25 = Bm25(Bm25Config(language="english"))
    return _bm25


def bm25_document(text: str) -> Tuple[List[int], List[float]]:
    sv = bm25().embed_document(text or "")
    return list(sv.indices), list(sv.values)


def bm25_query(text: str) -> Tuple[List[int], List[float]]:
    sv = bm25().embed_query(text or "")
    return list(sv.indices), list(sv.values)


_shared: Optional[Embedder] = None


def shared_embedder() -> Embedder:
    global _shared
    if _shared is None:
        _shared = Embedder()
    return _shared
