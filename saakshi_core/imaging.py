"""Image fingerprints and EXIF parsing shared by device and cloud.

Everything here is pure Pillow + numpy so it works offline.
"""

from __future__ import annotations

import hashlib
import io
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

import numpy as np
from PIL import ExifTags, Image, ImageOps

log = logging.getLogger("saakshi.imaging")

ImageSource = Union[str, Path, bytes, Image.Image]

# Editing apps whose name in EXIF "Software" suggests the photo was altered
EDITING_SOFTWARE = ("photoshop", "lightroom", "snapseed", "picsart", "gimp", "canva", "facetune", "remini")


@dataclass
class ExifInfo:
    has_exif: bool = False
    lat: Optional[float] = None
    lon: Optional[float] = None
    captured_at: Optional[str] = None  # ISO 8601, naive local time as written by the camera
    make: Optional[str] = None
    model: Optional[str] = None
    software: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def has_gps(self) -> bool:
        return self.lat is not None and self.lon is not None

    @property
    def edited_with(self) -> Optional[str]:
        if not self.software:
            return None
        s = self.software.lower()
        for name in EDITING_SOFTWARE:
            if name in s:
                return self.software
        return None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["has_gps"] = self.has_gps
        return d


def open_image(src: ImageSource) -> Image.Image:
    """Open an image and apply its EXIF orientation."""
    if isinstance(src, Image.Image):
        img = src
    elif isinstance(src, (bytes, bytearray)):
        img = Image.open(io.BytesIO(src))
    else:
        img = Image.open(str(src))
    img.load()
    try:
        img = ImageOps.exif_transpose(img)
    except Exception as exc:  # corrupt orientation tags should not block ingest
        log.debug("Ignoring unreadable EXIF orientation: %s", exc)
    return img


def _ratio_to_float(v: Any) -> float:
    try:
        return float(v)
    except TypeError:
        num, den = v
        return float(num) / float(den) if den else 0.0


def _dms_to_deg(dms: Any, ref: Any) -> Optional[float]:
    try:
        d, m, s = (_ratio_to_float(x) for x in dms)
    except Exception:
        return None
    deg = d + m / 60.0 + s / 3600.0
    ref = ref.decode() if isinstance(ref, bytes) else str(ref or "")
    if ref.upper() in ("S", "W"):
        deg = -deg
    return round(deg, 7)


def _parse_exif_datetime(value: Any) -> Optional[str]:
    if not value:
        return None
    if isinstance(value, bytes):
        value = value.decode(errors="ignore")
    value = str(value).strip().replace("\x00", "")
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M"):
        try:
            return datetime.strptime(value, fmt).isoformat()
        except ValueError:
            continue
    return None


def read_exif(src: ImageSource) -> ExifInfo:
    """Extract GPS, capture time and camera details. Never raises."""
    info = ExifInfo()
    try:
        if isinstance(src, Image.Image):
            img = src
        elif isinstance(src, (bytes, bytearray)):
            img = Image.open(io.BytesIO(src))
        else:
            img = Image.open(str(src))
        exif = img.getexif()
    except Exception:
        return info
    if not exif:
        return info
    info.has_exif = True
    info.make = _clean(exif.get(ExifTags.Base.Make))
    info.model = _clean(exif.get(ExifTags.Base.Model))
    info.software = _clean(exif.get(ExifTags.Base.Software))
    try:
        sub = exif.get_ifd(ExifTags.IFD.Exif)
    except Exception:
        sub = {}
    info.captured_at = _parse_exif_datetime(
        sub.get(ExifTags.Base.DateTimeOriginal) or sub.get(ExifTags.Base.DateTimeDigitized) or exif.get(ExifTags.Base.DateTime)
    )
    try:
        gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    except Exception:
        gps = {}
    if gps:
        lat = _dms_to_deg(gps.get(2), gps.get(1)) if gps.get(2) else None
        lon = _dms_to_deg(gps.get(4), gps.get(3)) if gps.get(4) else None
        # (0, 0) is what some apps write when location is off
        if lat is not None and lon is not None and not (abs(lat) < 1e-6 and abs(lon) < 1e-6):
            info.lat, info.lon = lat, lon
    return info


def _clean(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, bytes):
        v = v.decode(errors="ignore")
    v = str(v).strip().strip("\x00")
    return v or None


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Union[str, Path]) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dhash(img: Image.Image, size: int = 8) -> str:
    """64-bit difference hash as 16 hex chars. Survives resizing and recompression."""
    g = img.convert("L").resize((size + 1, size), Image.LANCZOS)
    px = np.asarray(g, dtype=np.int16)
    bits = (px[:, 1:] > px[:, :-1]).flatten()
    value = 0
    for b in bits:
        value = (value << 1) | int(b)
    return f"{value:0{size * size // 4}x}"


def hamming_hex(a: Optional[str], b: Optional[str]) -> int:
    """Bit distance between two hex hashes. Returns 64 when either is missing."""
    if not a or not b:
        return 64
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except ValueError:
        return 64


def thumbnail_bytes(img: Image.Image, max_side: int = 480, quality: int = 78) -> bytes:
    t = img.convert("RGB").copy()
    t.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    t.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def fit_within(data: bytes, limit: int) -> bytes:
    """A JPEG of at most `limit` bytes, keeping the original's EXIF (GPS, time, orientation).

    For storage services with a file size cap. Pixels keep their stored orientation,
    so the copied Orientation tag still applies."""
    src = Image.open(io.BytesIO(data))
    exif = src.info.get("exif")
    img = src.convert("RGB")
    out = data
    for side in (6000, 4800, 4000, 3200, 2400, 1600):
        img.thumbnail((side, side), Image.LANCZOS)  # each step shrinks the previous one
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=88, optimize=True, **({"exif": exif} if exif else {}))
        out = buf.getvalue()
        if len(out) <= limit:
            break
    return out


def compressed_bytes(img: Image.Image, max_side: int = 1600, quality: int = 82) -> bytes:
    """Bandwidth-friendly upload copy. EXIF is preserved separately in the metadata we send."""
    t = img.convert("RGB").copy()
    t.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    t.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def color_histogram_vector(img: Image.Image, bins: int = 8) -> list:
    """512-d joint RGB histogram (8x8x8), L2-normalised.

    Used as an offline fallback embedding when the CLIP model is not available.
    It is good at near-duplicate detection and weak at semantics.
    """
    small = img.convert("RGB").resize((96, 96), Image.BILINEAR)
    arr = np.asarray(small, dtype=np.uint8).reshape(-1, 3) // (256 // bins)
    idx = arr[:, 0].astype(np.int32) * bins * bins + arr[:, 1].astype(np.int32) * bins + arr[:, 2].astype(np.int32)
    hist = np.bincount(idx, minlength=bins ** 3).astype(np.float32)
    hist = np.sqrt(hist)  # soften dominant colours
    n = float(np.linalg.norm(hist)) or 1.0
    return (hist / n).tolist()


def image_size(img: Image.Image) -> Tuple[int, int]:
    return int(img.width), int(img.height)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def iso_to_ts(value: Optional[str]) -> Optional[float]:
    """Parse ISO 8601 (naive treated as UTC) to epoch seconds."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()
