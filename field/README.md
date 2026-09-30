# Saakshi Field (PS03 · Qdrant Edge)

Offline-first evidence app for a field officer's laptop or phone. See the [main README](../README.md) for the whole system.

```bash
python -m field --device dev-a --name "Officer Rina" --port 8101
```

| File | What it does |
|---|---|
| `app/memory.py` | Two Qdrant Edge shards: `local` (this device) and `mirror` (server copy from snapshots). Hybrid CLIP + BM25 search with RRF across both. |
| `app/service.py` | Capture pipeline (EXIF, site, faces, embedding, duplicate check, policy), search, edits, conflict resolution. No network code. |
| `app/policy.py` | Decides per item: sync, sync a face-blurred copy, hold, keep private, or skip as duplicate; priority and resolution tier. Every decision has reasons. |
| `app/sync.py` | Outbox push (vectors to Qdrant, photos to Impact), full/partial snapshot pull into the mirror, scroll fallback, background worker. |
| `app/conflicts.py` | Site status claims from different devices: agree, newer wins, or conflict. |
| `app/merge.py` | Field-level last-writer-wins merge of offline edits. |
| `app/store.py` | SQLite: outbox, activity log, site states, conflicts. |
| `app/faces.py` | OpenCV face detection and blurring on the device. |
| `app/static/` | The device UI. No CDN assets, so it loads with the network off. |

Data lives in `data/field/<device-id>/`: `memory/local`, `memory/mirror`, `media/`, `thumbs/`, `upload/` (blurred copies) and `device.sqlite3`.

Run without any network at all: `python -m field --device solo --no-sync`.
