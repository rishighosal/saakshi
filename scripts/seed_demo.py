"""Load photos into running devices so the demo never starts empty.

    python scripts/seed_demo.py                       # the sample photos (Wikimedia Commons) into device A and B
    python scripts/seed_demo.py --photos my_photos/   # your own photos into device A
    python scripts/seed_demo.py --for-video           # leave out the photos you drop in on camera
    python scripts/seed_demo.py --device-a http://127.0.0.1:8101 --device-b http://field-b:8101   # inside docker compose

The sample photos are real, openly licensed photos (credits in docs/PHOTOS.md), downloaded
once by scripts/fetch_sample_photos.py; the notes and projects are fictional.
Uses the field devices' HTTP API, so start them first (scripts/run_demo.py).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.fetch_sample_photos import DEFAULT_OUT, fetch, load_manifest

URLS = {"a": "http://127.0.0.1:8101", "b": "http://127.0.0.1:8102"}


def send(device: str, path: Path, note: str = "", status: str = "", project: str = "") -> None:
    r = httpx.post(
        f"{URLS[device].rstrip('/')}/api/evidence",
        files=[("files", (path.name, path.read_bytes(), "image/jpeg"))],
        data={"note": note, "status_claim": status, "project_id": project},
        timeout=180,
    )
    r.raise_for_status()
    res = r.json()["results"][0]
    if res.get("error"):
        print(f"  {device.upper()} {path.name:34s} → {res['error']}")
        return
    print(f"  {device.upper()} {path.name:34s} → {res.get('site_name')} · {res.get('sync_action')} · {(res.get('sync_reasons') or [''])[0]}")


def wait_until_sent(device: str, timeout_s: float = 90) -> None:
    """Let the device sync what it has, so a repeat shot is linked to a photo the server already has."""
    end = time.time() + timeout_s
    while time.time() < end:
        out = httpx.get(f"{URLS[device].rstrip('/')}/api/status", timeout=30).json().get("outbox") or {}
        if not out.get("queued") and not out.get("failed"):
            return
        time.sleep(2)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--photos", help="folder of your own photos (sent to device A)")
    ap.add_argument("--for-video", action="store_true", help="leave out the photos marked live_in_video (dropped in on camera)")
    ap.add_argument("--device-a", default=URLS["a"], help="URL of device A")
    ap.add_argument("--device-b", default=URLS["b"], help="URL of device B")
    args = ap.parse_args()
    URLS["a"], URLS["b"] = args.device_a, args.device_b
    if args.photos:
        folder = Path(args.photos)
        files = sorted(p for p in folder.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"))
        for f in files:
            send("a", f, f.stem.replace("_", " ").replace("-", " "))
        return
    out = fetch(DEFAULT_OUT)
    m = load_manifest()
    seeds = {x["file"]: x["seed"] for x in m["photos"] + m["derived"] if "seed" in x}
    held = []
    for name in m["seed_order"]:
        s = seeds[name]
        if args.for_video and s.get("live_in_video"):
            held.append(name)
            continue
        if not s.get("note") and not s.get("status"):
            wait_until_sent(s["device"])  # a plain repeat shot: the first one should be on the server
        send(s["device"], out / name, s.get("note", ""), s.get("status", ""), s.get("project", ""))
    if held:
        print("\nFor the video, drop these on device A yourself:", ", ".join(held))
    print("\nDone. The devices sync on their own when online; press 'Sync now' to hurry them.")


if __name__ == "__main__":
    main()
