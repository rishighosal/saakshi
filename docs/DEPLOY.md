# Deploying Saakshi

Saakshi has two halves. The **cloud side** (a Qdrant server, the Impact app, Cloudinary) runs once per organisation. The **field side** (the Saakshi Field app with its Qdrant Edge memory) runs on every officer's laptop or on a small field kit that officers' phones connect to.

| Option | For | Time |
|---|---|---|
| [A. Everything on one machine](#a-everything-on-one-machine-docker-compose) | demos, evaluation, development | 5 min |
| [B. Cloud side in the cloud](#b-cloud-side-qdrant-cloud--render--cloudinary) | a real NGO pilot | 20 min |
| [C. Field devices](#c-field-devices) | officers' laptops, field kits | 10 min per device |

## A. Everything on one machine (Docker Compose)

```bash
cp .env.example .env          # optional: add CLOUDINARY_URL for AI Vision, face blurring and transformations
docker compose up --build
```

| Service | URL | What it is |
|---|---|---|
| `qdrant` | http://localhost:6333/dashboard | Qdrant 1.19.1 in **cluster mode on one node**, so the collection gets one shard per project region |
| `impact` | http://localhost:8000 | the NGO office app (Cloudinary pipeline, dashboard, reports) |
| `field-a` | http://localhost:8101 | Officer Rina's device |
| `field-b` | http://localhost:8102 | Officer Arjun's device, with a 512 MB budget for originals |

Load sample photos into both devices: `docker compose exec field-a python scripts/seed_demo.py --device-b http://field-b:8101` (or drop your own photos in the UI).

Without `CLOUDINARY_URL` the Impact app stores files locally and skips AI Vision; everything on the field side works the same.

## B. Cloud side: Qdrant Cloud + Render + Cloudinary

1. **Qdrant Cloud.** Create a free cluster at [cloud.qdrant.io](https://cloud.qdrant.io); copy its URL and an API key. Saakshi checks `GET /cluster` on first use: with cluster mode on (Qdrant Cloud clusters run distributed), it creates the collection with **custom sharding, one shard key per project**; on a standalone server it falls back to one global shard. Devices work with either and the Sync tab shows which one is in use.
2. **Cloudinary.** Sign up, copy the API environment variable (`cloudinary://…`), and enable the **AI Vision** add-on (free tier). See [SETUP.md](SETUP.md).
3. **Impact on Render.** New → Blueprint → this repository (uses [`deploy/render.yaml`](../deploy/render.yaml)), or New → Web Service → Docker. Set `CLOUDINARY_URL`, `QDRANT_URL`, `QDRANT_API_KEY`, `INGEST_TOKEN` (a long random string), `PUBLIC_BASE_URL` (the Render URL). The free plan sleeps when idle and has a temporary disk; Impact rebuilds its index from Cloudinary on start (`POST /api/admin/rebuild`), because Cloudinary is the system of record.
4. Point devices at it: `QDRANT_URL`, `QDRANT_API_KEY`, `IMPACT_URL`, `INGEST_TOKEN` in each device's `.env`.

Any container host works the same way (Railway, Fly.io, Google Cloud Run, a VM with `docker compose up -d qdrant impact`).

## C. Field devices

### On a laptop (Windows, macOS, Linux)

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python scripts/download_models.py                      # once, while online: CLIP for on-device search
python -m field --device dev-rina --name "Officer Rina"
```

Open http://127.0.0.1:8101. The browser can install it as an app (it ships a web manifest and a service worker for the app shell). The app binds to `127.0.0.1` by default; nothing on the network can reach it unless you pass `--host 0.0.0.0`.

### On a field kit (Raspberry Pi 4/5 or any small Linux box)

For teams whose officers carry only phones: one kit per team, in the field office or the vehicle, with a power bank. Phones join the kit's Wi-Fi hotspot and open `http://<kit-ip>:8101`; the kit holds the memory and does the syncing when it has a link.

```bash
# Raspberry Pi OS Bookworm, 64-bit
sudo apt install -y python3-venv libgl1
git clone <your repo> ~/saakshi && cd ~/saakshi
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/download_models.py
cp .env.example .env    # set DEVICE_ID, DEVICE_NAME, QDRANT_URL, IMPACT_URL, INGEST_TOKEN
sudo cp deploy/saakshi-field.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now saakshi-field
```

Or with Docker on the kit: `docker build -f Dockerfile.field -t saakshi-field . && docker run -p 8101:8101 --env-file .env -v $PWD/data:/app/data saakshi-field`.

### Platform support

`qdrant-edge-py` 0.8.0 publishes wheels for these platforms (checked with `pip download --only-binary=:all:`):

| Platform | Wheel | Notes |
|---|---|---|
| Linux x86_64 | `manylinux_2_17_x86_64` | laptops, servers, CI |
| Linux ARM64 | `manylinux_2_28_aarch64` | Raspberry Pi 4/5 on 64-bit Raspberry Pi OS Bookworm, Ubuntu 20.04+ on ARM; needs glibc 2.28 or newer |
| macOS Apple silicon | `macosx_11_0_arm64` | |
| Windows x64 | `win_amd64` | |

There is no Android or iOS wheel, so on phones Saakshi runs as a web app served by a laptop or field kit. A native phone build would use Qdrant Edge's Rust core directly; that is the next step on the roadmap.

### Sizing

Measured in [BENCHMARKS.md](BENCHMARKS.md): the device's Qdrant Edge memory takes about 5 KB of disk per photo for vectors, index and payload (52 MB per 10,000 photos), plus the photos themselves. Originals are the big part; set `MEDIA_BUDGET_MB` and the device releases the oldest originals that are already synced and checked by HQ (their vectors, note and thumbnail stay searchable). With `EDGE_QUANTIZE=1` the search index also keeps int8 vectors, 4x smaller in RAM.

**On Windows**, add a fixed amount per shard. Qdrant preallocates its storage files (32 MB vector chunks, payload pages and write-ahead-log segments); Linux and macOS keep the unused part sparse, but NTFS allocates all of it. Before opening a shard, Saakshi marks its files sparse and releases the unused ranges ([`field/app/diskspace.py`](../field/app/diskspace.py)), so stored data costs what it does on Linux. What remains is the write-ahead log that Qdrant creates when a shard opens: two 32 MB segments per open shard, which are memory-mapped and cannot be released while the app runs. A device with its own shard and three region mirrors therefore holds about 256 MB of log on top of its data. This does not grow with the number of photos. Keep the data folder out of cloud-synced folders (`SAAKSHI_DATA_DIR`); they would upload the shard files as they change.

**Windows path length.** Qdrant Edge nests its files about 120 characters below each shard folder, and Windows without long-path support refuses paths of 260 characters or more. So keep the data folder short, for example `SAAKSHI_DATA_DIR=C:\saakshi-data`; the device warns at start and on its memory card when the folder is too deep. Enabling Windows long paths (`LongPathsEnabled`) also removes the limit.

## Security checklist

* `INGEST_TOKEN`: a long random value, the same on Impact and every device. Impact refuses device uploads without it, and it is also the **office key**: uploads, edits, project and site changes, campaign images, AI change descriptions and index rebuilds all require it (the dashboard asks once and keeps it in that browser). Reading the dashboard stays open, so donors and visitors can browse the evidence.
* `QDRANT_API_KEY`: always set for a server reachable from the internet (Qdrant Cloud requires one).
* Terminate HTTPS in front of Impact (Render does it for you).
* Field apps listen on `127.0.0.1` unless started with `--host 0.0.0.0` for a field kit; run a kit's hotspot with WPA2 and a password.
* Photos with faces leave a device only blurred unless consent is recorded; private items never leave it.
* `.env` is in `.gitignore`; never commit it.

## Operations

| Task | How |
|---|---|
| Back up the server | Qdrant collection snapshots (`POST /collections/saakshi_evidence/snapshots`); Cloudinary already holds every photo with its metadata |
| Rebuild Impact's index | `POST /api/admin/rebuild` (reads Cloudinary) |
| Reset a device | stop it, delete `data/field/<device-id>/`; its mirrors re-download on the next pull. Anything not yet synced is lost, so sync first |
| Move a device to other regions | Sync tab → Regions; mirrors of dropped regions are deleted on the next pull |
| Upgrade | pull the new code, `pip install -r requirements.txt`, restart; `qdrant-edge-py` stays pinned to the tested version |
