# How Saakshi compares

Two questions a reviewer should ask: *why not the tools NGOs already use?* and *why Qdrant Edge on the device, rather than a simpler store or the cloud?* Numbers here come from [BENCHMARKS.md](BENCHMARKS.md); everything else is a statement about how each approach works, not a claim that it is bad at what it was built for.

## 1. Against the ways field evidence is handled today

| | Messaging groups (e.g. WhatsApp) | Offline form apps (ODK Collect, KoboCollect) | Cloud photo / survey apps | **Saakshi** |
|---|---|---|---|---|
| Capture with no network | yes | yes | often limited | **yes** |
| Keeps GPS and time of the original | often lost: images sent as photos are recompressed and metadata is stripped | yes (recorded by the form) | usually | **yes, plus SHA-256 and dHash fingerprints at capture** |
| Search your own evidence offline | scroll the chat | browse saved forms | no | **hybrid search: picture + words, filters, geo, recency** |
| Search the **team's** evidence offline | only what reached your phone | not the photos | no | **yes: the device mirrors its regions' shards** |
| "Show me photos like this one" | no | no | sometimes, online | **yes, on the device (CLIP vectors, Qdrant recommend)** |
| Decides what to upload, and says why | no | no (uploads finalised forms) | no | **per-item policy with a plain-language reason** |
| Urgent reports before routine ones | no | no | no | **priority queue; vectors travel before photos** |
| Adapts to a 2G link | no | no | no | **measures the link; routine photos wait or go compressed** |
| Faces of people who did not consent | uploaded as is | uploaded as is | uploaded as is | **blurred on the device, or held for approval** |
| Re-uploads of the same shot | yes | yes | yes | **linked to the original instead** |
| Detects a photo reused from another project | no | no | rarely | **yes, in the cloud (hash, pHash, CLIP) and the verdict flows back to the device** |
| Conflicting reports from two officers | lost in the chat | last upload wins | last write wins | **flagged; a person decides; the decision syncs to every device** |

The simulated field day ([BENCHMARKS.md](BENCHMARKS.md#3-a-simulated-field-day)) puts numbers on the three rows that depend on connectivity: searches answered, how much of the team's evidence an officer can find, and how long an urgent photo takes to reach HQ.

## 2. Against other ways to build the device's memory

What the device needs: vector search over photos, keyword search over notes, filters (project, site, status, time, **distance**), re-ranking by recency and distance, persistence across restarts, and a way to **exchange data with a central server** that does not require re-embedding or a custom replication protocol.

| | NumPy / FAISS arrays | SQLite + hand-written code | Qdrant server only (cloud search) | **Qdrant Edge on the device** |
|---|---|---|---|---|
| Works with no network | yes | yes | **no** | **yes** |
| Approximate index for large memories | FAISS: yes; NumPy: no | no | yes | **yes, HNSW, built in the background (`optimize()`)** |
| Keyword + vector in one query | build it | build it | yes | **yes: sparse BM25 + dense CLIP, fused with RRF** |
| Payload and geo filters, re-scoring formulas, MMR | build it | partly (SQL filters) | yes | **yes, same query API as the server** |
| Persistence, crash safety | build it | yes | yes | **yes (WAL, segments)** |
| Sync with the central collection | build it | build it | n/a | **server shard snapshots load directly as device shards; partial snapshots send only changed segments** |
| Same vectors and tokenizer on device and server | up to you | up to you | n/a | **yes: one vector space end to end** |

The last two rows are why we chose Qdrant Edge. A regional shard on the server is, byte for byte, something the device can open. That turns synchronisation into moving files the engine already understands, and lets the device fall back to a cheap filtered scroll when only a few points changed.

### Measured on the same data

From [BENCHMARKS.md](BENCHMARKS.md) (512-d vectors, 200 queries, this repository's benchmark scripts):

* **Small memories (1k–10k photos): NumPy brute force is the fastest.** A matrix product over 10,000 vectors takes 0.5 ms, against 1.6 ms for the index. We report this because it is true; the device still uses Qdrant Edge for everything in the table above, and every device search takes under 2 ms either way.
* **Large memories (50k–100k photos): the HNSW index wins.** Brute force grows linearly (6.3 ms at 100k); the indexed search stays under 2 ms (1.2 ms at 100k) with recall@10 of 0.985 or better.
* **Qdrant server over HTTP on localhost** answers in 15–31 ms on the benchmark laptop (Windows). That is the best case for cloud search, with zero network distance. On a real 2G link a request adds hundreds of milliseconds to seconds, and with no link there is no answer.
* **Sync size:** keeping a region current after one new photo costs **about 3 KB** by delta pull; on Qdrant Cloud a partial snapshot re-sends the region (7.3 MB), about as much as re-downloading it (7.5 MB). A device that serves one of five regions downloads a quarter of what the whole collection would cost (7.2 MB against 29.4 MB).

## 3. What Saakshi does not do (yet)

* It does not run natively on Android or iOS; phones use the web app served by a laptop or field kit ([DEPLOY.md](DEPLOY.md#platform-support)).
* CLIP ViT-B/32 is a general model. It is good at "garbage", "saplings", "flooded road"; it is not a species classifier or a survival-rate estimator.
* The field-day comparison is a simulation with stated assumptions (network mix, photo sizes, photos per visit). The policy code in it is the production code; the day is synthetic.
