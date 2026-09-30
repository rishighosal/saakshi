"""Download the CLIP models once, while online, so the field app works offline.

    python scripts/download_models.py

Models go to ~/.cache/saakshi-models (override with SAAKSHI_MODEL_DIR).
A few hundred MB in total. Run this before any offline demo.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.pop("SAAKSHI_DISABLE_CLIP", None)

from PIL import Image

from saakshi_core.embeddings import Embedder, default_cache_dir


def main() -> None:
    print(f"Downloading CLIP models to {default_cache_dir()} …")
    t0 = time.time()
    e = Embedder()
    mode = e.load()
    if mode != "clip":
        print(f"\nCould not load CLIP: {e.error}\nCheck your internet connection and try again.")
        sys.exit(1)
    img = Image.new("RGB", (224, 224), (40, 130, 60))
    v = e.embed_image(img)
    t = e.embed_text("young saplings planted in soil")
    tags = e.zero_shot_tags(v)
    print(f"Ready in {time.time() - t0:.0f} s. Image vector {len(v)}-d, text vector {len(t)}-d, sample tags {tags}")
    print("The field app will now work with the network off.")


if __name__ == "__main__":
    main()
