"""Impact stays responsive while it ingests, and a device retry cannot process the same photo twice."""

import json
import threading
import time
import uuid

import httpx
import pytest
import uvicorn

from impact.app.main import create_app
from impact.app.service import ImpactService, ImpactSettings
from tests.fake_cloud import FakeCloudinary


class SlowCloudinary(FakeCloudinary):
    def upload(self, *args, **kwargs):
        time.sleep(1.5)  # a slow link to Cloudinary
        return super().upload(*args, **kwargs)


@pytest.fixture()
def server(tmp_path):
    settings = ImpactSettings(data_dir=tmp_path, ingest_token="tok")
    store = SlowCloudinary()
    app = create_app(settings, ImpactService(settings, store=store))
    port = 19000 + (uuid.uuid4().int % 1000)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            httpx.get(base + "/api/health", timeout=0.5)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    yield base, store
    srv.should_exit = True


def test_dashboard_answers_during_an_ingest_and_a_retry_is_not_processed_twice(server, photos):
    base, store = server
    photo = (photos / "pond-a_before.jpg").read_bytes()
    meta = json.dumps({"evidence_id": str(uuid.uuid4()), "file_name": "pond.jpg"})

    def send():
        return httpx.post(base + "/api/ingest", files={"file": ("pond.jpg", photo, "image/jpeg")}, data={"meta": meta},
                          headers={"X-Saakshi-Token": "tok"}, timeout=30)

    results = {}
    first = threading.Thread(target=lambda: results.setdefault("first", send()))
    first.start()
    time.sleep(0.5)  # the first request is now inside the slow upload

    t0 = time.perf_counter()
    assert httpx.get(base + "/api/status", timeout=5).status_code == 200
    assert time.perf_counter() - t0 < 0.8, "the dashboard waited for the ingest to finish"

    retry = send()  # the device lost the connection and tries again
    first.join()
    assert results["first"].status_code == 200
    assert retry.status_code == 409
    assert len(store.uploads) == 1
    # once the first one finished, a retry is answered from the database
    again = send()
    assert again.status_code == 200 and again.json().get("duplicate_request")
