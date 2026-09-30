"""Names shared by the edge shards, the Qdrant server collection and the cloud app.

Keeping these in one place is what makes a server snapshot loadable as an
edge shard: the named vectors must match exactly on both sides.
"""

# Named vectors
VECTOR_CLIP = "clip"          # dense, CLIP ViT-B/32 (image and text share this space)
VECTOR_BM25 = "bm25"          # sparse, BM25 with IDF modifier, from qdrant_edge.Bm25
CLIP_DIM = 512

# Qdrant server collection shared by all devices and the cloud app
COLLECTION_EVIDENCE = "saakshi_evidence"

# Point kinds
KIND_EVIDENCE = "evidence"
KIND_RESOLUTION = "resolution"

# Sync states shown in the device UI
SYNC_LOCAL_ONLY = "local_only"      # never leaves the device (user marked private)
SYNC_HELD = "held"                  # held by policy (privacy) until someone approves
SYNC_QUEUED = "queued"              # waiting in the outbox
SYNC_SYNCED = "synced"              # on the server
SYNC_SKIPPED = "skipped_duplicate"  # near-duplicate of something already synced

# Status claims a field officer can make about a site
STATUS_CLAIMS = ["planned", "in_progress", "completed", "needs_attention", "damaged"]

# Payload fields that get a payload index on edge and server
KEYWORD_INDEXES = ["project_id", "site_id", "device_id", "kind", "sync_state", "status_claim", "origin", "sha256"]
FLOAT_INDEXES = ["captured_ts", "updated_ts"]
GEO_INDEX = "loc"
