# Saakshi Impact (PS02 · Cloudinary)

The NGO office's evidence platform. See the [main README](../README.md) for the whole system.

```bash
python -m impact --port 8000
```

| File | What it does |
|---|---|
| `app/service.py` | Ingest pipeline for web uploads and field devices; before/after pairing; campaign images; search; reports; rebuild from Cloudinary. |
| `app/cloud.py` | Cloudinary upload with `media_metadata`, `faces`, `phash`, `colors`, `quality_analysis`; video via `upload_large`; AI Vision tagging and general mode. Includes a local-files fallback for development. |
| `app/transforms.py` | Delivery URLs: thumbnails, video frames, before/after composites, campaign formats, downloads; and `explain()`, which turns a transformation string into plain steps for provenance. |
| `app/integrity.py` | Integrity checks and score: metadata, location, dates, editing, quality, reuse. |
| `app/pairing.py` | Chooses before/after photos per site and describes the change from AI tags. |
| `app/vectors.py` | Shared Qdrant collection (or a numpy fallback) for semantic search; writes cloud results back to device points. |
| `app/db.py` | SQLite index, rebuildable from Cloudinary context and tags. |
| `app/static/` | Dashboard: overview, projects, evidence, map, before/after, asset provenance, search, upload, donor report. |

Deploy with the repository's `Dockerfile` (see [docs/SETUP.md](../docs/SETUP.md)).
