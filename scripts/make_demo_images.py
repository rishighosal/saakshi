"""Generate synthetic field photos with real EXIF (GPS + capture time).

The tests use these, with the projects in tests/data/synthetic_projects.json.
The demo uses real photos instead (scripts/fetch_sample_photos.py).

    python scripts/make_demo_images.py --out demo_photos
"""

from __future__ import annotations

import argparse
import math
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple

from PIL import ExifTags, Image, ImageDraw, ImageFilter

W, H = 1600, 1200


def _to_dms(value: float) -> Tuple[float, float, float]:
    value = abs(value)
    d = int(value)
    m_full = (value - d) * 60
    m = int(m_full)
    s = round((m_full - m) * 60, 2)
    return (float(d), float(m), float(s))


def exif_for(lat: Optional[float], lon: Optional[float], when: datetime, make: str = "Saakshi", model: str = "Synthetic Camera") -> Image.Exif:
    ex = Image.Exif()
    ex[ExifTags.Base.Make] = make
    ex[ExifTags.Base.Model] = model
    stamp = when.strftime("%Y:%m:%d %H:%M:%S")
    ex[ExifTags.Base.DateTime] = stamp
    ex.get_ifd(ExifTags.IFD.Exif)[ExifTags.Base.DateTimeOriginal] = stamp
    if lat is not None and lon is not None:
        gps = ex.get_ifd(ExifTags.IFD.GPSInfo)
        gps[1] = "N" if lat >= 0 else "S"
        gps[2] = _to_dms(lat)
        gps[3] = "E" if lon >= 0 else "W"
        gps[4] = _to_dms(lon)
    return ex


def _ground(rng: random.Random, base=(118, 104, 86)) -> Image.Image:
    img = Image.new("RGB", (W, H), base)
    d = ImageDraw.Draw(img)
    # sky band
    d.rectangle([0, 0, W, int(H * 0.28)], fill=(176, 198, 214))
    # far wall / buildings
    for i in range(7):
        x = i * W // 7
        hgt = rng.randint(120, 260)
        tone = rng.randint(150, 190)
        d.rectangle([x, int(H * 0.28) - hgt // 2, x + W // 7 - 8, int(H * 0.36)], fill=(tone, tone - 12, tone - 25))
    # texture
    for _ in range(9000):
        x, y = rng.randint(0, W - 1), rng.randint(int(H * 0.36), H - 1)
        v = rng.randint(-22, 22)
        d.point((x, y), fill=(base[0] + v, base[1] + v, base[2] + v))
    return img


def litter_scene(rng: random.Random, amount: int = 90) -> Image.Image:
    img = _ground(rng)
    d = ImageDraw.Draw(img)
    colors = [(225, 40, 50), (40, 90, 210), (240, 240, 240), (30, 30, 30), (250, 200, 40), (60, 160, 70), (240, 120, 30)]
    for _ in range(amount):
        x, y = rng.randint(60, W - 60), rng.randint(int(H * 0.42), H - 40)
        r = rng.randint(12, 46)
        c = rng.choice(colors)
        if rng.random() < 0.5:
            d.ellipse([x - r, y - r // 2, x + r, y + r // 2], fill=c)
        else:
            d.polygon([(x - r, y), (x, y - r // 2), (x + r, y + r // 3), (x + r // 3, y + r // 2)], fill=c)
    return img.filter(ImageFilter.GaussianBlur(0.8))


def clean_scene(rng: random.Random, plants: int = 0) -> Image.Image:
    img = _ground(rng, base=(128, 116, 98))
    d = ImageDraw.Draw(img)
    for i in range(plants):
        x = 140 + (i % 9) * 160 + rng.randint(-20, 20)
        y = int(H * 0.5) + (i // 9) * 170 + rng.randint(-10, 10)
        d.rectangle([x - 3, y - 70, x + 3, y], fill=(90, 70, 40))
        for k in range(5):
            ang = -math.pi / 2 + (k - 2) * 0.5
            d.ellipse([x + 45 * math.cos(ang) - 20, y - 70 + 40 * math.sin(ang) - 12, x + 45 * math.cos(ang) + 20, y - 70 + 40 * math.sin(ang) + 12], fill=(48, 150, 62))
    return img.filter(ImageFilter.GaussianBlur(0.6))


def pond_scene(rng: random.Random, dirty: bool) -> Image.Image:
    img = _ground(rng, base=(96, 120, 78))
    d = ImageDraw.Draw(img)
    water = (70, 96, 70) if dirty else (64, 128, 150)
    d.ellipse([160, int(H * 0.42), W - 160, H - 80], fill=water)
    if dirty:
        for _ in range(70):
            x, y = rng.randint(260, W - 260), rng.randint(int(H * 0.5), H - 160)
            d.ellipse([x - 14, y - 8, x + 14, y + 8], fill=rng.choice([(230, 230, 230), (210, 50, 40), (40, 60, 180), (200, 180, 60)]))
    else:
        for _ in range(20):
            x = rng.randint(170, W - 170)
            d.rectangle([x, int(H * 0.36), x + 6, int(H * 0.44)], fill=(60, 110, 50))
    return img.filter(ImageFilter.GaussianBlur(0.7))


def mangrove_scene(rng: random.Random, grown: bool) -> Image.Image:
    img = _ground(rng, base=(92, 84, 72))
    d = ImageDraw.Draw(img)
    d.rectangle([0, int(H * 0.62), W, H], fill=(104, 118, 128))  # tidal water
    n = 26 if grown else 10
    for i in range(n):
        x = 80 + i * (W - 160) // n + rng.randint(-15, 15)
        top = int(H * (0.38 if grown else 0.52))
        d.line([x, top, x, int(H * 0.64)], fill=(70, 55, 35), width=6)
        s = 60 if grown else 24
        d.ellipse([x - s, top - s // 2, x + s, top + s // 2], fill=(40, 130, 60))
    return img.filter(ImageFilter.GaussianBlur(0.6))


def save(img: Image.Image, path: Path, lat: Optional[float], lon: Optional[float], when: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=88, exif=exif_for(lat, lon, when))


def generate(out: Path, seed: int = 7) -> list:
    rng = random.Random(seed)
    now = datetime.now().replace(microsecond=0)
    made = []
    # Clean Streets Kolkata: two spots, before and after
    spots = [("park-street-corner", 22.5530, 88.3520), ("gariahat-drain", 22.5186, 88.3657)]
    for name, lat, lon in spots:
        save(litter_scene(rng), out / f"{name}_before.jpg", lat, lon, now - timedelta(days=2, hours=3))
        save(clean_scene(rng, plants=0), out / f"{name}_after.jpg", lat + 0.00005, lon + 0.00004, now - timedelta(hours=5))
        made += [f"{name}_before.jpg", f"{name}_after.jpg"]
    # Pond A
    save(pond_scene(rng, dirty=True), out / "pond-a_before.jpg", 22.4871, 88.3791, now - timedelta(days=20))
    save(pond_scene(rng, dirty=False), out / "pond-a_after.jpg", 22.4869, 88.3790, now - timedelta(days=1))
    made += ["pond-a_before.jpg", "pond-a_after.jpg"]
    # Mangroves: embankment north
    save(mangrove_scene(rng, grown=False), out / "gosaba-embankment_before.jpg", 22.1713, 88.8020, now - timedelta(days=60))
    save(mangrove_scene(rng, grown=True), out / "gosaba-embankment_after.jpg", 22.1711, 88.8022, now - timedelta(days=2))
    made += ["gosaba-embankment_before.jpg", "gosaba-embankment_after.jpg"]
    # A photo with no GPS (WhatsApp-style) and one reused copy of an old photo
    img = clean_scene(rng, plants=18)
    img.save(out / "no-gps_saplings.jpg", "JPEG", quality=85)
    made.append("no-gps_saplings.jpg")
    reused = Image.open(out / "pond-a_after.jpg").copy()
    save(reused, out / "reused_pond-photo.jpg", 22.5530, 88.3520, now - timedelta(hours=1))
    made.append("reused_pond-photo.jpg")
    return made


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="demo_photos")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    made = generate(Path(args.out), args.seed)
    print(f"Wrote {len(made)} synthetic photos to {args.out}/")
    for m in made:
        print("  ", m)


if __name__ == "__main__":
    main()
