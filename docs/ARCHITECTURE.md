# Architecture

## Components

```
┌──────────────── Saakshi Field (one per device) ───────────────┐         ┌──────────── Saakshi Impact ────────────┐
│ FastAPI on localhost + offline web UI                          │         │ FastAPI + dashboard                    │
│                                                                │         │                                        │
│ service.py   capture · search · edit · conflicts (no network)  │  photo  │ service.py  ingest pipeline            │
│ memory.py    Qdrant Edge: local · region mirrors · deltas      │ ──────▶ │ cloud.py    Cloudinary + AI Vision     │
│ policy.py    what leaves the device, and why                   │         │ integrity.py  pairing.py  transforms.py│
│ sync.py      push · link monitor · pull planner · fallback     │         │ db.py       SQLite index (rebuildable) │
│ assistant.py Ask: grounded answers with citations              │         │                                        │
│ store.py     SQLite: outbox, activity, site state, conflicts   │         │ vectors.py  Qdrant (shared) or numpy   │
└──────────────────────────┬─────────────────────────────────────┘         └───────────────┬────────────────────────┘
                           │ vectors (upsert)                                              │ enrichment (set_payload)
                           ▼                                                               ▼
                    ┌──────────── Qdrant server: saakshi_evidence (custom sharding: one shard per region) ──────────┐
                    │ named vectors: clip (512, cosine, HNSW m=32) · bm25 (sparse, IDF) · payload indexes           │
                    └──── per region: snapshot → device mirror · changed points → delta shard · partial snapshot ───┘
```

`saakshi_core` holds what both sides must agree on: vector names and sizes, the BM25 model, the CLIP models, EXIF parsing and fingerprints, and project/site logic.

## Evidence point

Every photo is one point, with the same id on the device, in Qdrant and in Cloudinary (`context.evidence_id`, public id `saakshi/<id>`).

| Field | Meaning |
|---|---|
| `kind` | `evidence` or `resolution` (a human decision on a conflict) |
| `project_id`, `site_id`, `site_name` | placement from GPS |
| `device_id`, `origin` | who captured it; `field` or `impact` (web upload) |
| `captured_at`, `captured_ts`, `lat`, `lon`, `loc` | from EXIF; `loc` is geo-indexed |
| `note`, `status_claim`, `consent`, `private` | officer input, editable |
| `field_ts`, `field_by`, `version` | per-field edit clocks for merging |
| `sha256`, `dhash` | fingerprints of the original |
| `faces`, `tags` | on-device face count and CLIP zero-shot tags |
| `sync_state`, `sync_action`, `sync_reasons`, `priority`, `tier` | the policy decision (device copy) |
| `cloud` | written by Impact: `public_id`, `thumb_url`, `integrity_score`, `integrity_level`, `ai_tags`, `caption` |
| `updated_ts` | bumped on every write; drives incremental processing |

## Device memory (Qdrant Edge)

Three kinds of `EdgeShard` per device, under `data/field/<device>/memory/`:

* **local** (mutable): created with `EdgeShard.create`, holds what this device captured. Payload indexes on the keyword fields, `captured_ts`, `updated_ts` and a geo index on `loc`. HNSW `m=32`, `ef_construct=256`, built by `optimize()` in the background once 200 new points arrive; searched with `hnsw_ef=512`. Optional int8 scalar quantization (`EDGE_QUANTIZE=1`).
* **`mirror/<region>`** (read-only copy of the server): one per region the device serves, created by `EdgeShard.unpack_snapshot` from `GET /collections/saakshi_evidence/shards/<id>/snapshot` and refreshed with partial snapshots (`snapshot_manifest()` → `POST …/snapshot/partial/create` → `update_from_snapshot`).
* **`delta/<region>`**: points changed on the server since the mirror's last snapshot, fetched one by one. Emptied whenever a snapshot is applied.

Search runs the same `QueryRequest` on every shard and merges by id, newest copy first (local, delta, mirror). With CLIP: dense `clip` and sparse `bm25` prefetches fused with `Fusion.Rrf(k=60)`, optionally re-scored with a `Formula` (exponential recency decay on `captured_ts`, Gaussian distance decay with `GeoDistance` on `loc`) or diversified with `Mmr`. Duplicate detection uses `Query.Nearest` on `clip` across all shards, confirmed with dHash when running without CLIP.

## Server layout (`saakshi_core/qdrant_server.py`)

* **Cluster mode available** (Qdrant Cloud; `docker compose` here): the collection uses `sharding_method=custom`; each project is a shard key (`unassigned` for photos without one). Writes carry `shard_key_selector`; `GET /collections/<c>/cluster` maps shard ids to keys for snapshot URLs.
* **Standalone server**: one shard (`shard_number=1`), treated as a single global region.
* The regional collection is created with `indexing_threshold=500` KB and 4 segments per shard, so filled segments are sealed and a partial snapshot need only carry the small segment with new points. Qdrant Cloud re-sends the whole region in a partial snapshot (measured), which is why the device prefers changed points (a few KB each) for up to 100 changes. HNSW `m=32`, `ef_construct=256`, the same as the device, because a device's mirror is the server's shard.

## Sync protocol

**Push** (`SyncEngine.push_once`), every few seconds when a server answers:

1. Take due outbox rows, highest priority first.
2. Vectors: read point + vectors from the local shard, upsert them to the server in one batch per region (`wait=True`). Edits (`op=update`) first merge with the server copy field by field.
3. Media: POST the photo to Impact `/api/ingest` with its metadata. Which file depends on the decision and the link: the original, a 1600 px copy (metered or slow link), or the face-blurred copy made on the device. On a constrained link (measured below 128 KB/s, or the simulated 2G mode) photos with priority < 65 wait up to 45 minutes, then go compressed. Impact's response (`cloud`) is written into the local point straight away.
4. Failures back off exponentially (5 s → 5 min); vectors and media retry independently.

**Link monitor** (`LinkMonitor`): a rolling estimate of upload speed from real transfers of at least 32 KB (smaller ones measure latency, not bandwidth).

**Pull** (`SyncEngine.pull_once`), every 20 s, for each region in the device's subscriptions (mirrors of dropped regions are deleted):

1. No mirror yet → full shard snapshot.
2. Otherwise `_plan_pull` counts points on the server with `updated_ts` newer than the region's watermark (`POST /points/count` with the shard key):
   * reconcile due (15 min on a good link, 60 on a slow one) → partial snapshot;
   * 0 changed → nothing;
   * ≤ 100 changed (and the delta shard under 400 points) → **delta pull**: filtered `POST /points/scroll` with vectors into the delta shard (about 3 KB per point);
   * more → partial snapshot (the delta shard is folded in and emptied).
3. Scan mirror and delta points with `updated_ts` above the last watermark, oldest capture first:
   * a colleague's evidence → site status claims are evaluated, the thumbnail is cached for offline display;
   * a resolution → matching open conflicts close and the site takes the decided status;
   * the device's own evidence → cloud enrichment (`cloud`) and newer field edits are copied into the local shard and re-indexed, so AI Vision tags become searchable offline.
4. If the server refuses snapshots (4xx), the device switches to scroll mode for that region.

Clock skew between devices can hide a point from the timestamp filter; the periodic partial snapshot is what guarantees convergence.

## Policy (`field/app/policy.py`)

Order of rules, first match wins where it returns:

1. `private` → **local_only**.
2. Near-duplicate (≥ threshold, default 95%) of something already on the server → **skip_duplicate**, linked instead of uploaded.
3. Priority = 40 + 35 for urgent words + 20 for `damaged`/`needs_attention` + up to 20 for novelty − 10 without GPS.
4. Faces without consent → **sync_blurred** (blur on device) or **hold** (wait for approval), depending on the privacy mode.
5. Metered link and priority < 70 → compressed copy.

The sync engine adds the link-dependent part at send time (see Push above), and `FieldService.enforce_storage_budget` releases originals over `MEDIA_BUDGET_MB`, oldest first, only when they are synced and checked by HQ.

## Conflicts and merges

* **Site status** (`conflicts.py`): same status → agree; different statuses from different devices within 6 h → conflict; otherwise newer evidence wins. A newer claim closes older open conflicts at that site. A human decision becomes a `resolution` point (priority 90) so every device converges.
* **Edits** (`merge.py`): `note`, `status_claim`, `consent`, `private` each carry a timestamp; when two copies meet, each field keeps its newest value.

## Cloud pipeline (`impact/app/service.py`)

1. Read EXIF (web uploads) or trust the device's reading of the original (field uploads; the blurred copy has no EXIF).
2. Project = smallest project area containing the GPS point; site = known site within 150 m, else a new site at that spot.
3. Cloudinary upload with `media_metadata`, `faces`, `phash`, `colors`, `quality_analysis` into `saakshi/<project>/<site>`; context carries every evidence field. Videos use `upload_large` with `resource_type=video`; GPS and creation time come from the video's metadata.
4. AI Vision `ai_vision_tagging` with the taxonomy's tag definitions and `ai_vision_general` for a caption. If AI Vision fails, CLIP zero-shot tags are used and the source is recorded.
5. Reuse: SHA-256 equality, pHash/dHash within 6 bits, or CLIP cosine ≥ 0.975 against every stored asset.
6. Integrity score (`integrity.py`): fail −35 (reuse −50), warn −10; any fail → flagged, < 80 or two warnings → needs review.
7. Write back: Cloudinary context and tags (`integrity_*`, `ai_*`), and the shared Qdrant point's `cloud` payload.
8. Re-pair the site (`pairing.py`) and build the composite URL.

## Cloudinary features used

| Feature | Where |
|---|---|
| Upload API: `asset_folder`, `context`, `tags`, `media_metadata`, `faces`, `phash`, `colors`, `quality_analysis` | ingest |
| `upload_large` for video, `so_auto` frame extraction | video evidence |
| Analyze API: AI Vision tagging and general | tags, captions, change descriptions |
| `c_fill,g_auto` smart crops, `c_pad`, `l_<public_id>` + `fl_layer_apply` overlays | before/after composites |
| `l_text` overlays with background and border, `c_fit` wrapping | labels, captions, credits |
| `e_blur_faces`, `e_improve:outdoor` | privacy, campaign images |
| `f_auto,q_auto`, `fl_attachment` | delivery, downloads |
| `add_context`, `add_tag`, Search API (`tags=saakshi`) | write-back, rebuild |

## API summary

Field (`http://127.0.0.1:8101`): `GET /api/status`, `GET /api/system`, `POST /api/evidence`, `GET /api/evidence`, `GET/PATCH /api/evidence/{id}`, `POST /api/evidence/{id}/approve`, `GET /api/evidence/{id}/similar`, `GET /api/evidence/{id}/more-like-this`, `GET /api/search` (recency, near_site, radius_km, boost_near, diverse, verified_only, status, source), `POST /api/ask`, `GET /api/outbox`, `POST /api/sync/now`, `POST /api/network` (online, slow, offline), `POST /api/settings`, `GET/POST /api/regions`, `GET /api/conflicts`, `POST /api/conflicts/{id}/resolve`, `GET /api/sites`, `GET /api/sites/{id}/timeline`, `GET /api/activity`, `POST /api/benchmark/live`, `GET /api/benchmark/results`, `GET /media/thumb/{id}`, `GET /media/original/{id}`.

Impact (`http://127.0.0.1:8000`): `GET /api/overview`, `GET /api/projects`, `POST /api/projects`, `GET /api/projects/{id}`, `GET /api/projects/{id}/report`, `GET /api/assets`, `GET /api/assets/{id}`, `PATCH /api/evidence/{id}`, `POST /api/upload`, `POST /api/ingest` (devices, token), `POST /api/pairs`, `POST /api/pairs/{id}/describe`, `POST /api/campaign`, `PATCH /api/sites/{id}`, `GET /api/search`, `POST /api/admin/rebuild`. Interactive docs at `/docs` on both.
