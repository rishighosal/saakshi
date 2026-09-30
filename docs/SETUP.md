# Setup

This gets the full system running on one laptop in about 15 minutes, then shows how to deploy the cloud app so others can open it.

## 1. Accounts (free)

**Cloudinary** (needed for PS02 features)

1. Sign up at [cloudinary.com](https://cloudinary.com/users/register_free).
2. Console → **Dashboard** → copy the **API environment variable**. It looks like `cloudinary://123456:abcDEF@your-cloud`. Put it in `.env` as `CLOUDINARY_URL`.
3. Console → **Add-ons** → search **AI Vision** → subscribe to the free plan. Without it, tags fall back to CLIP and change descriptions are unavailable; the dashboard shows a banner.
4. The free plan has small add-on quotas. Saakshi calls AI Vision twice per photo (tags + caption) and once per before/after description, and caches every result. Test with a few photos before loading a big batch. The dashboard shows the remaining quota when Cloudinary reports it.

**Qdrant** (needed for PS03 sync)

* Local, simplest for development: `docker compose up -d qdrant` → `QDRANT_URL=http://127.0.0.1:6333`, no key. The compose file starts Qdrant in **cluster mode on one node**, which enables custom sharding: the collection gets one shard per project region and devices mirror only their regions.
* Cloud, needed if the Impact app is deployed: create a free cluster at [cloud.qdrant.io](https://cloud.qdrant.io), copy its URL and an API key into `QDRANT_URL` and `QDRANT_API_KEY`. The collection is created automatically on first use.
* A plain standalone server (`docker run -p 6333:6333 qdrant/qdrant`) also works: Saakshi detects that cluster mode is off and uses one global shard. If a server refuses snapshot downloads, devices switch to scroll-based mirroring on their own and log it.

## 2. Install

Python 3.10–3.12 (3.11 recommended).

```bash
python -m venv .venv
source .venv/bin/activate          # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env               # Windows: copy .env.example .env
```

Edit `.env`: `CLOUDINARY_URL`, `QDRANT_URL` (+ key if cloud), and a long random `INGEST_TOKEN`.

Download the CLIP models once while online (they are cached under `~/.cache/saakshi-models`):

```bash
python scripts/download_models.py
```

## 3. Run

```bash
python scripts/run_demo.py
```

| App | URL |
|---|---|
| Saakshi Impact (NGO office) | http://127.0.0.1:8000 |
| Field device A, "Officer Rina" | http://127.0.0.1:8101 |
| Field device B, "Officer Arjun" | http://127.0.0.1:8102 |

Or run them separately:

```bash
python -m impact --port 8000
python -m field --device dev-a --name "Officer Rina" --port 8101
python -m field --device dev-b --name "Officer Arjun" --port 8102
```

Each device keeps its data in `data/field/<device-id>/` (Qdrant Edge shards, photos, thumbnails, SQLite outbox). Delete a folder to reset that device. `data/impact/` holds the cloud app's index; it can be rebuilt from Cloudinary.

Load sample photos: `python scripts/seed_demo.py` (downloads 15 openly licensed photos from Wikimedia Commons once, into `demo_photos/commons/`; credits in [PHOTOS.md](PHOTOS.md)), or your own: `python scripts/seed_demo.py --photos path/to/folder`. The demo projects in `saakshi_core/data/projects.json` cover the sample photos; for your own photos, add a project that covers them there (before the first run) or with `POST /api/projects` on Impact: radius in metres, dates that include the photos.

## 4. Use a phone as a field device

Run a device bound to your Wi-Fi address and open it from the phone's browser:

```bash
python -m field --device phone-1 --name "Officer Rina" --host 0.0.0.0 --port 8101
```

Then browse to `http://<laptop-ip>:8101` on the phone and upload straight from the camera. The phone is the camera and screen; the laptop is the edge device holding the memory. For the offline demo, switch the laptop's Wi-Fi off, or use the **Online / 2G / Offline** switch in the device's top bar. For a permanent setup (a Raspberry Pi field kit with a systemd service), see [DEPLOY.md](DEPLOY.md).

## 5. Deploy Impact online

The repository ships a `Dockerfile` for the cloud app.

**Render** (free web service): New → Web Service → connect the repo → Docker → add environment variables `CLOUDINARY_URL`, `QDRANT_URL`, `QDRANT_API_KEY`, `INGEST_TOKEN`, `ORG_NAME`, `PUBLIC_BASE_URL` (the Render URL). Free instances sleep when idle and the disk is temporary; Saakshi rebuilds its index from Cloudinary on start, so nothing is lost.

**Hugging Face Spaces**: create a Docker Space, push the repo, set the same variables as secrets. Spaces expect port 7860: add `PORT=7860`.

Then point the field devices at it: `IMPACT_URL=https://your-app.onrender.com` and the same `QDRANT_URL`/`INGEST_TOKEN`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Device shows "Colour-histogram fallback" | CLIP models not downloaded. Run `python scripts/download_models.py` online, restart the device. |
| `onnxruntime` fails to install on Windows | Install the [Microsoft Visual C++ Redistributable](https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist), use Python 3.11 64-bit. |
| Banner: "AI Vision is not answering" | Enable the AI Vision add-on in Cloudinary; check the quota. Tags fall back to CLIP meanwhile. |
| Photos land in "No location" | The file has no GPS. Phones forwarded through WhatsApp lose it; copy originals by cable or Google Drive. Turn on location for the camera app. |
| "Pull failed" in the device activity log | Check `QDRANT_URL`/`QDRANT_API_KEY`. If snapshots are refused, the device switches to scroll mode by itself. |
| Sync tab says "global layout" | The server is not in cluster mode, so there is one shard for everything. Use `docker compose up -d qdrant` (cluster mode) or Qdrant Cloud to get one shard per region. An existing collection keeps its layout: delete it to switch. |
| Benchmarks tab is empty | Run `python -m bench.run_all` (or `--quick --skip-server`) once; the tab reads `docs/benchmarks/results.json`. |
| Port already in use | Stop the other process or pass `--port`. |
| Device never syncs | Is **auto sync** ticked and the network pill green? `INGEST_TOKEN` must match between the device and Impact. |
| Windows: "cannot find the path specified" from a shard | The data folder is too deep for Windows paths (the device's memory card says so). Set `SAAKSHI_DATA_DIR` to a short folder such as `C:\saakshi-data`, or enable Windows long paths. |
