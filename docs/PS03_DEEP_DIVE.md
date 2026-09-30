# PS03 deep dive: every requirement, where it lives, how it is tested, how to see it

> **PS03 · AI-Powered Edge Memory & Intelligence Platform.** "Build an offline-first AI application powered by Qdrant Edge that can remember, retrieve, operate offline, and synchronize intelligently when connected … a meaningful edge-to-cloud AI workflow, rather than simply running a local vector database."

Saakshi Field is that application for NGO field teams: officers photograph work sites (a cleaned drain, a planted embankment, a breached bund) in places where the network comes and goes. This page maps each goal in the brief to the code, the tests and the demo, then lists the engineering decisions we measured on the way.

## At a glance

| Goal in the brief | What Saakshi does | Code | Tests | See it |
|---|---|---|---|---|
| Searchable semantic memory on the device | Every photo is a point in the device's own Qdrant Edge shard: CLIP image vector, BM25 sparse vector of the note, payload with GPS, time, site, status, faces, fingerprints | `field/app/memory.py`, `saakshi_core/schema.py` | `test_capture_places_photo_on_project_and_site`, `test_offline_search_finds_notes_fast` | Memory tab; How it works tab |
| Low-latency vector and hybrid search, no network | Dense + sparse prefetch fused with RRF; recency and distance re-scoring with Qdrant formulas; geo and payload filters; MMR; merged across local, regional mirror and delta shards; query plan returned to the UI | `DeviceMemory.search` | `test_search_returns_query_plan_and_reasons`, `test_recency_boost_changes_the_plan`, `test_geo_radius_filter_keeps_only_nearby_evidence`, `test_more_like_this` | Search tab with the network set to Offline |
| Reason over local information | **Ask**: questions in plain words answered from the device's memory with a citation for every claim; optional local LLM (Ollama) phrases the answer, still offline | `field/app/assistant.py` | `test_assistant_site_briefing_with_citations`, `test_assistant_outbox_and_search_intents` | Ask tab |
| Decide what stays local and what syncs | Per-item policy with a plain-language reason (private, faces, repeat shots, urgency, novelty, metered link); link-aware sending (vectors first, routine photos wait on 2G, compressed copies); storage budget; region subscriptions decide what comes **down** | `field/app/policy.py`, `SyncEngine.push_once`, `FieldService.enforce_storage_budget`, `subscribed_regions` | `tests/test_policy.py` (11 tests), `test_slow_link_defers_low_priority_photos_but_sends_vectors`, `test_storage_budget_only_releases_originals_safe_in_the_cloud` | Sync tab (every row has its reasons); Sync policy card |
| Intermittent connectivity, keep operating offline | SQLite outbox with priorities and exponential backoff; vectors and photos retried separately; three network modes (online, simulated 2G, offline); a link monitor measures real transfers | `field/app/store.py`, `field/app/sync.py` (`LinkMonitor`) | `test_network_switch_and_sync_now_offline`, `test_network_modes`, `test_link_monitor_classifies_links` | Online / 2G / Offline switch in the top bar |
| Sync edge devices with Qdrant Server | Push: batched upserts into the device's **region shard** (custom sharding, one shard key per project). Pull per region: full snapshot the first time, then **only changed points** (filtered scroll) or a **partial snapshot** (changed segments), whichever is cheaper; nothing when nothing changed (the device counts changes first); periodic reconcile; scroll fallback | `saakshi_core/qdrant_server.py`, `SyncEngine.pull_once`, `_plan_pull`, `_pull_delta`, `_pull_snapshot` | `test_edge_to_cloud_round_trip`, `test_pull_picks_delta_or_snapshot`, `test_regional_layout_devices_mirror_only_their_regions`, `test_scroll_fallback_when_snapshots_are_refused` | Sync tab: "Last pull" per region with its mode, size and reason |
| Evolving memory, updates, conflicting information | Field-level merge with per-field clocks; conflicting site reports within a window become conflicts; newer evidence closes them; a human decision becomes a point that syncs everywhere; HQ verdicts flow back into the device's own points; site timelines | `field/app/merge.py`, `field/app/conflicts.py`, `FieldService.site_timeline` | `tests/test_conflicts.py` (8 tests), `test_conflict_detected_and_resolution_propagates`, `test_site_timeline_shows_evolving_knowledge`, `test_original_arriving_after_its_copy_flips_the_verdict` | Sites tab: conflict cards and per-site history |
| Interface for memory, search, sync status, activity | Offline web app (no CDN), installable: Memory, Search (with query plan), Ask, Sync (queue with reasons, regions, pull details), Sites (timelines, conflicts), How it works (live system map), Benchmarks (measured + live), Activity | `field/app/static/` | UI script parse check in CI | all tabs; **Tour** button |
| A meaningful edge-to-cloud AI workflow | Capture offline → policy → vectors to the region shard → photo to the cloud → Cloudinary AI Vision + integrity checks → verdict written into the shared point → back to the officer's device on the next pull, searchable offline ("HQ-verified only") and used by Ask | whole repo; `impact/app/vectors.py` (`enrich`) | `test_edge_to_cloud_round_trip`, `test_reused_photo_is_flagged_by_the_cloud` | a photo card gains "HQ: verified 100" after sync |

The benchmark suite (`bench/`) measures the parts that make it an *edge* system: search latency and recall on the device, bytes needed to stay in sync, and a simulated field day against a cloud app and an offline queue app. Results: [BENCHMARKS.md](BENCHMARKS.md).

## 1. Memory on the device

**One point per photo**, with the same id on the device, on the server and in Cloudinary:

* `clip`: 512-d CLIP ViT-B/32 image vector (cosine). Text queries use the CLIP text encoder, so "flooded road" finds photos of flooded roads with no caption.
* `bm25`: sparse vector of the note, site name and tags (`qdrant_edge.Bm25`, the same tokenizer the server uses, IDF modifier).
* payload: project, site, device, GPS (`loc`, geo-indexed), capture time, note, status claim, faces, consent, privacy flag, SHA-256 and dHash, the sync decision with its reasons, per-field edit clocks, and `cloud` (HQ's verdict, AI tags, caption) once it comes back.

**Shards on the device** (`DeviceMemory`):

| Shard | Holds | How it changes |
|---|---|---|
| `local` | what this device captured | written by capture and edits |
| `mirror/<region>` | the server's shard for each region this device serves | unpacked from a full shard snapshot, updated with partial snapshots |
| `delta/<region>` | points changed on the server since the mirror's last snapshot | filled by delta pulls; emptied when the next snapshot folds them in |

Search runs the same request on every shard and merges by id, newest copy first (local, then delta, then mirror).

**Indexing.** Qdrant Edge searches exactly until `optimize()` builds the HNSW index. The device calls it in the background once 200 new points have arrived (`FieldService.maintenance`). We use a denser graph than the default (`m=32`, `ef_construct=256`) and search with `hnsw_ef=512`: on 100,000 clustered photo vectors this is the difference between recall@10 of **0.60** (defaults) and **0.985** (tuned), at 1.2 ms per search on a laptop ([BENCHMARKS.md](BENCHMARKS.md#1-search-on-the-device)). `EDGE_QUANTIZE=1` adds int8 scalar quantization for devices short on RAM.

**Storage budget.** Originals are the heavy part. Over the budget (`MEDIA_BUDGET_MB`), the device releases the oldest originals that are **both synced and checked by HQ**; the point, its vectors and the thumbnail stay, so the photo remains searchable offline. Private and held items are never touched. `/media/original/<id>` then answers 410 with the cloud URL.

## 2. Search and reasoning without a network

`GET /api/search` builds one Qdrant query:

1. **Candidates**: `Prefetch(Nearest(clip))` and `Prefetch(Nearest(bm25))`, each with the filters (project, status, source, "HQ-verified only" through the nested key `cloud.integrity_level`, geo radius), fused with `Fusion.Rrf(k=60)`.
2. **Re-scoring** (optional): a `Formula` multiplies the fused score by an exponential **recency decay** on `captured_ts` and/or a Gaussian **distance decay** from a site (`GeoDistance` on `loc`).
3. **Diversity** (optional): `Mmr(λ=0.5)` so ten near-identical shots of one drain do not fill the page.
4. The response carries the **query plan** (each step in words), the search time, the query-embedding time, and for each hit the matching words and whether it matched visually.

**Ask** (`field/app/assistant.py`) recognises the question (a site's status, conflicts, what is waiting to sync, HQ verdicts, what is near me, or a general search), retrieves the matching points with the same search, and writes the answer itself with numbered citations. If `OLLAMA_URL`/`OLLAMA_MODEL` point at a small local model, the model phrases the answer from the same numbered records, still offline; the citations are kept either way. The answer can only contain what the device's memory contains.

## 3. Deciding what stays and what goes

**Up** (`field/app/policy.py`, `decide()`), first match wins:

1. Marked private → stays on the device, forever.
2. A repeat of a shot already on the server (≥ 95% similar, within 15 minutes, no new note) → linked to it, not uploaded. The same spot hours later is a new observation (that is how before/after works), and a similar photo with its own note or status still goes.
3. Priority = 40, +35 for urgent words (flood, breach, collapsed…), +20 for a damaged / needs-attention report, up to +20 for novelty, −10 without GPS.
4. Faces without recorded consent → a face-blurred copy is made on the device and only that leaves (or the item is held for approval, per the privacy setting).
5. Metered link → full resolution only for priority ≥ 70.

**How it travels** (`SyncEngine.push_once`): vectors for every due item first (about 4 KB each: the team can find the item within seconds of any link), then photos by priority. The `LinkMonitor` measures real uploads; below 128 KB/s the link counts as constrained: routine photos (priority < 65) wait for a better link for up to 45 minutes, then go as compressed copies; urgent photos go at once.

**Down** (regions): a device serves the regions (projects) its officer works in (Sync tab). It mirrors only those shards, so an officer in the Sundarbans carries Sundarbans evidence, not Kolkata's.

## 4. Intermittent connectivity

* The outbox (`field/app/store.py`) survives restarts; each row tracks vectors and photo separately and backs off from 5 s to 5 min on failure.
* The top-bar switch sets the device **Online**, **2G** (a simulated 24 KB/s link: transfers are slowed to that rate so the link monitor and the policy react as they would in the field) or **Offline** (sync stops; capture, search, Ask and edits continue).
* When the link returns, the background worker drains the queue in priority order and pulls the regions.

## 5. Synchronising with Qdrant Server

**Server layout** (`saakshi_core/qdrant_server.py`). On a cluster-mode server (Qdrant Cloud, or `docker compose up` here) the collection uses **custom sharding with one shard key per project region**; on a standalone server it falls back to one global shard. Devices detect which and adapt; nothing else changes.

**Push**: points are grouped by region and upserted with `shard_key_selector`. Edits merge with the server copy field by field first.

**Pull**, per region, choosing the cheapest correct option each time (`SyncEngine._plan_pull`):

| Situation | What the device does | Measured cost |
|---|---|---|
| First time | download the region's shard snapshot, `EdgeShard.unpack_snapshot` | 7.2 MB for 2,000 photos |
| Nothing changed (count of points with a newer `updated_ts` is 0) | nothing | 0 bytes |
| A few points changed (≤ 100) | filtered scroll of just those points into the region's delta shard | **about 3 KB per point** |
| Many points changed | partial snapshot: POST the mirror's `snapshot_manifest()`, apply with `update_from_snapshot` | on Qdrant Cloud the whole region (7.4 MB for 50 new points, against 145 KB by delta) |
| Every 15 min on a good link (60 on a slow one) | partial snapshot to reconcile deletions and anything a skewed device clock hid from the timestamp filter; the delta shard is emptied | as above |
| Server refuses snapshots | scroll-based mirroring | per point |

**Why deltas carry the load** (measured in `bench/bench_sync.py` against Qdrant Cloud): a partial snapshot re-sends every *segment* the server does not recognise from the device's manifest. The regional collection is created with a low indexing threshold (500 KB) and 4 segments per shard so that, on a server that matches manifests, only the small segment with new points travels. On Qdrant Cloud we measured that partial snapshots re-send the whole region (7.3 MB for one new photo), so the device pulls changed points one by one (**3.2 KB** for one photo, 145 KB for 50) and keeps snapshots for the first pull and for reconciling. The HNSW settings match the device's, because a device's mirror *is* the server's shard.

## 6. Evolving memory, updates and conflicts

* **Edits** (`merge.py`): `note`, `status_claim`, `consent` and `private` each carry a timestamp and an author. When two copies meet, each field keeps its newest value; the merge is logged.
* **Conflicting reports** (`conflicts.py`): two devices reporting different statuses for the same site within 6 hours raise a conflict; the same device changing its mind does not; newer evidence outside the window wins and closes older conflicts. A person's decision becomes a `resolution` point with high priority, so every device converges on it.
* **Knowledge that arrives later**: HQ's verdict (AI tags, caption, integrity) is written into the shared point; on the next pull the device copies it into its own point and re-indexes it, so "verified" and the AI tags become searchable offline. If an original photo reaches the cloud after a copy of it, the verdicts flip (`test_original_arriving_after_its_copy_flips_the_verdict`).
* **Site timelines** show all of this in order: photos, status reports, conflicts, decisions, HQ checks.

## 7. The interface

| Tab | Shows |
|---|---|
| Memory | own and colleagues' evidence with sync state, reasons, HQ verdicts |
| Search | search box, options (recency, near a site, radius, rank vs filter, status, source, varied, HQ-verified), the query plan and timing |
| Ask | questions in plain words, answers with clickable citations, the engine and time |
| Sync | link quality and speed, server layout, last pull per region (mode, bytes, reason), bytes sent/received, region subscriptions, the queue with every decision's reasons |
| Sites | conflict cards to resolve, site cards, per-site history |
| How it works | live map: shards on the device with counts and sizes, the link, the server's regions |
| Benchmarks | measured headline numbers and charts; a live device-vs-server test |
| Activity | every capture, decision, push, pull, merge and HQ check |

The item drawer shows the decision's reasons, HQ's verdict, visually similar items and "more like this" with a **Not this** button (Qdrant recommend with negative examples).

## 8. The edge-to-cloud workflow, end to end

```
officer (offline)            device (Qdrant Edge)                  server (Qdrant, per-region shards)     cloud (Impact + Cloudinary)
photo + note ──────────────▶ CLIP + BM25, faces, fingerprints
                             policy: blur? repeat? urgent?
                             local shard ─ searchable now
       link returns ───────▶ vectors first ───────────────────────▶ region shard
                             photo (or blurred / compressed copy) ─────────────────────────────────────────▶ AI Vision tags + caption
                                                                                                             reuse / area / date checks
                                                                   ◀──────────────── verdict written into the point
                             pull: delta or partial snapshot ◀──── region shard
                             own point updated: "HQ: verified 100"
colleague's device ◀──────── same region shard: sees this photo, offline, with HQ's verdict
```

## Engineering decisions we measured

| Question | What we measured | Decision |
|---|---|---|
| Is the default HNSW good enough on the device? | recall@10 at 100k photos: 0.60 with Qdrant's defaults | `m=32`, `ef_construct=256`, `hnsw_ef=512`: 0.985 recall, 1.2 ms on a laptop |
| Brute force or index? | NumPy is faster up to 10k photos; at 100k it takes 6.3 ms vs 1.2 ms | keep Qdrant Edge; index in the background once memory grows |
| Is a partial snapshot the cheapest update? | on Qdrant Cloud a partial snapshot re-sends the region (7.3 MB for one new photo); a filtered scroll costs 3.2 KB per point | adaptive pull: delta up to 100 changed points, snapshot above, reconcile every 15 min |
| Why were small uploads flagged as a slow link? | a 4 KB vector upload measures latency, not bandwidth | the link monitor ignores transfers under 32 KB |
| Deferring routine photos on 2G: forever? | in the remote-island simulation, routine photos waiting for a good link could wait all day | defer for at most 45 min, then send a compressed copy: 86.5% of photos reach HQ, as many as the cloud app, with far less 2G data |
| Should a device carry every region? | whole collection 29.4 MB vs one region 7.2 MB (5 regions, 2,000 photos each) | custom sharding by project; devices subscribe to regions |
