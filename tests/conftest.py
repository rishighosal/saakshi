import os
import sys
from pathlib import Path

import pytest

# Tests never download models: use the deterministic fallback embedder
os.environ.setdefault("SAAKSHI_DISABLE_CLIP", "1")
# ...and use the projects the synthetic photos were made for (scripts/make_demo_images.py)
os.environ["SAAKSHI_PROJECTS_FILE"] = str(Path(__file__).resolve().parent / "data" / "synthetic_projects.json")


def pytest_configure(config):
    # Windows without long paths: Qdrant Edge nests its files ~120 characters below a shard
    # folder, and pytest's own temp folders are deep, so use a short one (pytest empties it)
    if os.name == "nt" and not config.option.basetemp:
        config.option.basetemp = str(Path.home() / ".saakshi-pytest")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def photos(tmp_path_factory) -> Path:
    """Synthetic field photos with real GPS/time EXIF (see scripts/make_demo_images.py)."""
    from scripts.make_demo_images import generate

    out = tmp_path_factory.mktemp("photos")
    generate(out)
    return out


def _auth() -> dict:
    key = os.environ.get("QDRANT_TEST_API_KEY")
    return {"api-key": key} if key else {}


@pytest.fixture(scope="session")
def qdrant_api_key():
    """API key for the test servers (e.g. Qdrant Cloud); None for local servers."""
    return os.environ.get("QDRANT_TEST_API_KEY") or None


@pytest.fixture(scope="session")
def qdrant_url() -> str:
    """URL of a real Qdrant server for sync tests. Set QDRANT_TEST_URL to enable."""
    url = os.environ.get("QDRANT_TEST_URL")
    if not url:
        pytest.skip("set QDRANT_TEST_URL (e.g. http://127.0.0.1:6333) to run sync tests")
    import httpx

    try:
        httpx.get(url, headers=_auth(), timeout=10.0).raise_for_status()
    except Exception:
        pytest.skip(f"Qdrant not reachable at {url}")
    return url


@pytest.fixture(scope="session")
def qdrant_cluster_url() -> str:
    """A Qdrant server in cluster mode (enables regional shard keys). Set QDRANT_CLUSTER_TEST_URL."""
    url = os.environ.get("QDRANT_CLUSTER_TEST_URL")
    if not url:
        pytest.skip("set QDRANT_CLUSTER_TEST_URL to a cluster-mode Qdrant to run regional sync tests")
    import httpx

    try:
        r = httpx.get(url.rstrip("/") + "/cluster", headers=_auth(), timeout=10.0)
        if r.json().get("result", {}).get("status") != "enabled":
            pytest.skip("server is not in cluster mode")
    except Exception:
        pytest.skip(f"Qdrant not reachable at {url}")
    return url
