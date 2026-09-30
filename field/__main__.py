"""Run a Saakshi Field device.

    python -m field --device dev-a --name "Officer Rina" --port 8101
    python -m field --device dev-b --name "Officer Arjun" --port 8102

Each device keeps its own data in data/<device-id>/.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import uvicorn

from saakshi_core.config import data_dir, env, env_float
from saakshi_core.schema import COLLECTION_EVIDENCE

from .app.main import create_app
from .app.policy import PolicySettings
from .app.service import FieldSettings


def main() -> None:
    ap = argparse.ArgumentParser(description="Saakshi Field: offline-first evidence device")
    ap.add_argument("--device", default=env("DEVICE_ID", "dev-a"), help="device id, e.g. dev-a")
    ap.add_argument("--name", default=env("DEVICE_NAME"), help="display name, e.g. 'Officer Rina'")
    ap.add_argument("--port", type=int, default=int(env("FIELD_PORT", "8101")))
    ap.add_argument("--host", default=env("FIELD_HOST", "127.0.0.1"))
    ap.add_argument("--data-dir", default=None, help="where device data lives (default ./data)")
    ap.add_argument("--no-sync", action="store_true", help="never talk to the network")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    root = Path(args.data_dir) if args.data_dir else data_dir() / "field"
    settings = FieldSettings(
        device_id=args.device,
        device_name=args.name or args.device,
        data_root=root,
        qdrant_url=None if args.no_sync else env("QDRANT_URL"),
        qdrant_api_key=env("QDRANT_API_KEY"),
        collection=env("QDRANT_COLLECTION", COLLECTION_EVIDENCE),
        impact_url=None if args.no_sync else env("IMPACT_URL", "http://127.0.0.1:8000"),
        ingest_token=env("INGEST_TOKEN"),
        sync_interval_s=env_float("SYNC_INTERVAL_S", 4.0),
        pull_interval_s=env_float("PULL_INTERVAL_S", 20.0),
        conflict_window_s=env_float("CONFLICT_WINDOW_HOURS", 6.0) * 3600,
        policy=PolicySettings(
            duplicate_threshold=env_float("DUPLICATE_THRESHOLD", 0.95),
            privacy_mode=env("PRIVACY_MODE", "blur"),
            media_budget_mb=env_float("MEDIA_BUDGET_MB", 2048.0),
        ),
        quantized=env("EDGE_QUANTIZE", "0") in ("1", "true", "yes"),
        ollama_url=env("OLLAMA_URL"),
        ollama_model=env("OLLAMA_MODEL"),
    )
    app = create_app(settings, start_sync=not args.no_sync)
    print(f"\n  Saakshi Field · {settings.device_name}\n  Open http://{args.host}:{args.port}\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
