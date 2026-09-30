"""Face detection and blurring on the device (OpenCV Haar cascade, fully offline).

Haar cascades are old but tiny and dependable on CPU; that matters more on a
field laptop than a few points of recall. Detection runs on a downscaled copy.
"""

from __future__ import annotations

import logging
from typing import List, Tuple

import numpy as np
from PIL import Image, ImageFilter

log = logging.getLogger("saakshi.field.faces")

Box = Tuple[int, int, int, int]  # x, y, w, h in original pixels

_cascade = None
_unavailable = False


def _get_cascade():
    global _cascade
    if _cascade is None:
        import cv2

        _cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    return _cascade


def available() -> bool:
    return not _unavailable


def detect_faces(img: Image.Image, max_side: int = 900) -> List[Box]:
    """Face boxes in original pixels. Returns [] (and logs once) if OpenCV cannot detect faces."""
    global _unavailable
    if _unavailable:
        return []
    try:
        import cv2

        _get_cascade()
    except Exception as exc:  # missing OpenCV, or a build without Haar cascades (OpenCV 5)
        _unavailable = True
        log.warning("Face detection disabled: %s. Install opencv-python-headless<5.", exc)
        return []
    scale = 1.0
    work = img.convert("L")
    if max(work.size) > max_side:
        scale = max_side / float(max(work.size))
        work = work.resize((int(work.width * scale), int(work.height * scale)), Image.BILINEAR)
    arr = np.asarray(work, dtype=np.uint8)
    arr = cv2.equalizeHist(arr)
    min_size = max(24, int(min(arr.shape) * 0.04))
    found = _get_cascade().detectMultiScale(arr, scaleFactor=1.1, minNeighbors=6, minSize=(min_size, min_size))
    boxes: List[Box] = []
    for (x, y, w, h) in (found if len(found) else []):
        boxes.append((int(x / scale), int(y / scale), int(w / scale), int(h / scale)))
    return boxes


def blur_faces(img: Image.Image, boxes: List[Box], pad: float = 0.25) -> Image.Image:
    """Return a copy with each face region heavily blurred."""
    out = img.convert("RGB").copy()
    for (x, y, w, h) in boxes:
        px, py = int(w * pad), int(h * pad)
        x0, y0 = max(0, x - px), max(0, y - py)
        x1, y1 = min(out.width, x + w + px), min(out.height, y + h + py)
        region = out.crop((x0, y0, x1, y1))
        radius = max(8, (x1 - x0) // 6)
        out.paste(region.filter(ImageFilter.GaussianBlur(radius)), (x0, y0))
    return out
