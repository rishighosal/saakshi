"""Run Saakshi Impact (the cloud dashboard).

    python -m impact --port 8000

Configure Cloudinary, Qdrant and the ingest token in .env (see .env.example).
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import uvicorn

from saakshi_core.config import data_dir, env, env_bool, env_float
from saakshi_core.schema import COLLECTION_EVIDENCE

from .app.main import create_app
from .app.service import ImpactSettings


def settings_from_env(data: Path) -> ImpactSettings:
    return ImpactSettings(
        data_dir=data,
        org_name=env("ORG_NAME", "Green Delta Collective (demo NGO)"),
        cloudinary_url=env("CLOUDINARY_URL"),
        cloud_name=env("CLOUDINARY_CLOUD_NAME"),
        api_key=env("CLOUDINARY_API_KEY"),
        api_secret=env("CLOUDINARY_API_SECRET"),
        folder=env("CLOUDINARY_FOLDER", "saakshi"),
        ai_vision=env_bool("AI_VISION_ENABLED", True),
        qdrant_url=env("QDRANT_URL"),
        qdrant_api_key=env("QDRANT_API_KEY"),
        collection=env("QDRANT_COLLECTION", COLLECTION_EVIDENCE),
        ingest_token=env("INGEST_TOKEN"),
        public_base=env("PUBLIC_BASE_URL", ""),
        llm_base_url=env("LLM_BASE_URL"),
        llm_api_key=env("LLM_API_KEY"),
        llm_model=env("LLM_MODEL"),
        pair_min_gap_hours=env_float("PAIR_MIN_GAP_HOURS", 1.0),
        max_image_bytes=int(env_float("CLOUDINARY_MAX_IMAGE_MB", 10) * 1024 * 1024),
        public_demo=env_bool("PUBLIC_DEMO", False),
        demo_video_url=env("DEMO_VIDEO_URL", ""),
        repo_url=env("REPO_URL", ""),
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Saakshi Impact: evidence platform on Cloudinary")
    ap.add_argument("--port", type=int, default=int(env("PORT", env("IMPACT_PORT", "8000"))))
    ap.add_argument("--host", default=env("IMPACT_HOST", "127.0.0.1"))
    ap.add_argument("--data-dir", default=None)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    root = Path(args.data_dir) if args.data_dir else data_dir() / "impact"
    app = create_app(settings_from_env(root))
    print(f"\n  Saakshi Impact\n  Open http://{args.host}:{args.port}\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
