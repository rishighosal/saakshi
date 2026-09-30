"""Download the demo's sample photos: real, openly licensed photos from Wikimedia Commons.

    python scripts/fetch_sample_photos.py                 # into demo_photos/commons
    python scripts/fetch_sample_photos.py --out my_folder

The originals keep their camera metadata (GPS, capture time), which the demo needs.
Two extra files are made from them: a WhatsApp-style copy without metadata and a
re-posted copy of another project's photo (see scripts/sample_photos.json).
Authors and licences: docs/PHOTOS.md. Wikimedia asks scripts to say who they are:
set SAAKSHI_CONTACT to your email or project URL.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import time
from pathlib import Path

import httpx
from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = Path(__file__).resolve().parent / "sample_photos.json"
DEFAULT_OUT = ROOT / "demo_photos" / "commons"
REPO_URL = "https://github.com/rishighosal/saakshi"


def load_manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _without_metadata(src: Path, long_side: int, quality: int, crop: float = 0.0) -> bytes:
    img = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    if crop:
        dx, dy = int(img.width * crop), int(img.height * crop)
        img = img.crop((dx, dy, img.width - dx, img.height - dy))
    img.thumbnail((long_side, long_side), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)  # no exif= argument: saved without metadata
    return buf.getvalue()


def derive(out: Path, d: dict) -> Path:
    dest = out / d["file"]
    if not dest.exists():
        src = out / d["from"]
        if d["how"] == "whatsapp":
            data = _without_metadata(src, 1600, 80)
        elif d["how"] == "repost":
            data = _without_metadata(src, 2048, 88, crop=0.03)
        else:
            raise ValueError(f"unknown derivation {d['how']!r}")
        dest.write_bytes(data)
    return dest


def fetch(out: Path = DEFAULT_OUT, quiet: bool = False) -> Path:
    """Download what is missing (checked against Commons' SHA-1) and make the derived copies."""
    m = load_manifest()
    out.mkdir(parents=True, exist_ok=True)
    contact = os.environ.get("SAAKSHI_CONTACT") or REPO_URL
    headers = {"User-Agent": f"SaakshiSampleFetch/1.0 ({contact}) a few openly licensed demo photos"}
    with httpx.Client(headers=headers, timeout=120, follow_redirects=True) as client:
        for p in m["photos"]:
            dest = out / p["file"]
            if dest.exists() and _sha1(dest.read_bytes()) == p["sha1"]:
                continue
            r = client.get(p["url"])
            r.raise_for_status()
            if _sha1(r.content) != p["sha1"]:
                raise SystemExit(f"{p['file']}: the download does not match Commons' SHA-1; not saved")
            dest.write_bytes(r.content)
            if not quiet:
                print(f"  {p['file']:36s} {len(r.content) / 1e6:4.1f} MB  {p['license']}, {p['author']}")
            time.sleep(1.0)  # be gentle with Wikimedia's servers
    for d in m["derived"]:
        derive(out, d)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="folder for the photos")
    args = ap.parse_args()
    out = fetch(Path(args.out))
    n = len(load_manifest()["photos"])
    print(f"{n} photos from Wikimedia Commons (+2 derived copies) in {out}. Credits: docs/PHOTOS.md")


if __name__ == "__main__":
    sys.exit(main())
