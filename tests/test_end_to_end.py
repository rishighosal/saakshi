"""Full edge → cloud workflow against a real Qdrant server.

Two field devices and the Impact app (with a stand-in for Cloudinary):
devices push vectors to Qdrant and photos to Impact, pull full then partial
snapshots, see each other's evidence offline, get conflicts, resolve them,
and receive the cloud's integrity verdict for their own photos.

Run with:  QDRANT_TEST_URL=http://127.0.0.1:6333 pytest tests/test_end_to_end.py
"""

import threading
import time
import uuid

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from field.app.main import create_app as field_app
from field.app.service import FieldSettings
from impact.app.main import create_app as impact_app
from impact.app.service import ImpactService, ImpactSettings
from tests.fake_cloud import FakeCloudinary


@pytest.fixture()
def stack(tmp_path, qdrant_url, qdrant_api_key):
    collection = f"saakshi_test_{uuid.uuid4().hex[:8]}"
    fake = FakeCloudinary()
    fake.names = {}
    settings = ImpactSettings(data_dir=tmp_path / "impact", qdrant_url=qdrant_url, qdrant_api_key=qdrant_api_key, collection=collection, ingest_token="tok")
    svc = ImpactService(settings, store=fake)
    app = impact_app(settings, svc)
    port = 18000 + (uuid.uuid4().int % 1000)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(50):
        try:
            httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=0.5)
            break
        except Exception:
            time.sleep(0.1)

    def device(dev):
        return TestClient(field_app(FieldSettings(device_id=dev, device_name=dev, data_root=tmp_path / "field", qdrant_url=qdrant_url, qdrant_api_key=qdrant_api_key,
                                                  collection=collection, impact_url=f"http://127.0.0.1:{port}", ingest_token="tok"),
                                    start_sync=False))

    with device("dev-a") as a, device("dev-b") as b:
        yield a, b, TestClient(app), fake
    server.should_exit = True
    try:
        httpx.delete(f"{qdrant_url}/collections/{collection}", headers={"api-key": qdrant_api_key} if qdrant_api_key else {}, timeout=15)
    except Exception:
        pass


def up(c, path, **form):
    return c.post("/api/evidence", files=[("files", (path.name, path.read_bytes(), "image/jpeg"))], data=form).json()["results"][0]


def test_edge_to_cloud_round_trip(stack, photos):
    a, b, impact, fake = stack
    # Device A captures two photos of the same site and syncs
    up(a, photos / "park-street-corner_before.jpg", note="garbage at the corner", status_claim="needs_attention")
    up(a, photos / "park-street-corner_after.jpg", note="corner cleared", status_claim="completed")
    r = a.post("/api/sync/now").json()
    assert r["push"]["vectors"] == 2 and r["push"]["media"] == 2, r["push"]
    assert r["pull"].get("mode") == "full", r["pull"]

    # The cloud stored, analysed and paired them
    ov = impact.get("/api/overview").json()
    assert ov["totals"]["evidence"] == 2 and ov["totals"]["from_field"] == 2
    proj = impact.get("/api/projects/clean-streets-kolkata").json()
    assert len(proj["pairs"]) == 1

    # Device B pulls: sees A's evidence in its mirror and can search it offline
    r = b.post("/api/sync/now").json()
    assert r["pull"]["pulled"] and r["pull"]["changed"] >= 2
    b.post("/api/network", json={"offline": True})
    hits = b.get("/api/search", params={"q": "corner cleared"}).json()["results"]
    assert hits and hits[0]["source"] == "mirror"
    b.post("/api/network", json={"offline": False})

    # A pulls again (only the two points HQ enriched) and receives the cloud's verdict on its own photos
    r = a.post("/api/sync/now").json()
    # "none" when the verdict was already in A's first snapshot (HQ answered before A pulled)
    assert r["pull"]["mode"] in ("delta", "partial", "none"), r["pull"]
    mine = a.get("/api/evidence", params={"include_mirror": False}).json()["items"]
    assert all((i.get("cloud") or {}).get("integrity_level") for i in mine)


def test_conflict_detected_and_resolution_propagates(stack, photos):
    a, b, impact, fake = stack
    up(a, photos / "pond-a_after.jpg", note="pond looks clean", status_claim="completed")
    a.post("/api/sync/now")
    # B reports the same site as damaged, from a photo taken at nearly the same time
    up(b, photos / "pond-a_after.jpg", note="bank collapsed", status_claim="damaged")
    b.post("/api/sync/now")
    conflicts = b.get("/api/conflicts").json()["items"]
    assert conflicts and conflicts[0]["state"] == "open"

    # B decides; A receives the decision and closes its own copy of the conflict
    b.post(f"/api/conflicts/{conflicts[0]['id']}/resolve", json={"choice": "local"})
    b.post("/api/sync/now")
    a.post("/api/sync/now")
    sites = {s["site_id"]: s for s in a.get("/api/conflicts").json()["sites"]}
    assert sites["pond-a"]["status"] == "damaged"
    assert all(c["state"] == "resolved" for c in a.get("/api/conflicts").json()["items"])


def test_reused_photo_is_flagged_by_the_cloud(stack, photos):
    a, b, impact, fake = stack
    up(a, photos / "pond-a_after.jpg", note="pond after clean-up")
    a.post("/api/sync/now")
    up(b, photos / "reused_pond-photo.jpg", note="street cleaned today")
    b.post("/api/sync/now")
    flagged = impact.get("/api/assets", params={"level": "flagged"}).json()["items"]
    assert len(flagged) == 1
    reuse = [c for c in flagged[0]["integrity"]["checks"] if c["key"] == "reuse"][0]
    assert reuse["status"] == "fail" and "Pond Revival" in reuse["message"]


def test_video_upload_is_placed_and_analysed(stack, photos):
    a, b, impact, fake = stack
    fake_video = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048
    r = impact.post("/api/upload", files=[("files", ("walkthrough.mp4", fake_video, "video/mp4"))], data={"note": "site walkthrough"},
                    headers={"X-Saakshi-Token": "tok"}).json()
    v = r["results"][0]
    assert v["media_type"] == "video"
    assert v["lat"] and abs(v["lat"] - 22.553) < 1e-3  # from the video's own metadata
    assert "/video/upload/" in v["thumb_url"]


def test_scroll_fallback_when_snapshots_are_refused(stack, photos):
    a, b, impact, fake = stack
    up(a, photos / "gosaba-embankment_before.jpg", note="bare embankment")
    a.post("/api/sync/now")
    # Pretend device B's server refused snapshots earlier
    b.app.state.service.db.set("pull_mode", "scroll")
    r = b.post("/api/sync/now").json()
    assert r["pull"]["mode"] == "scroll" and r["pull"]["changed"] >= 1
    assert r["pull"]["bytes"] > 0  # the fallback's downloads count towards "received" too
    assert b.get("/api/status").json()["sync"]["counters"]["bytes_downloaded"] >= r["pull"]["bytes"]
    hits = b.get("/api/search", params={"q": "embankment"}).json()["results"]
    assert hits and hits[0]["source"] == "mirror"


def test_original_arriving_after_its_copy_flips_the_verdict(stack, photos):
    a, b, impact, fake = stack
    # The copy (captured later, different project) syncs first...
    up(b, photos / "reused_pond-photo.jpg", note="street cleaned today")
    b.post("/api/sync/now")
    assert impact.get("/api/assets", params={"level": "flagged"}).json()["items"] == []
    # ...then the genuine original, captured a day earlier at the pond, arrives
    up(a, photos / "pond-a_after.jpg", note="pond after clean-up")
    a.post("/api/sync/now")
    flagged = impact.get("/api/assets", params={"level": "flagged"}).json()["items"]
    assert [f["note"] for f in flagged] == ["street cleaned today"]


def test_slow_link_defers_low_priority_photos_but_sends_vectors(stack, photos):
    a, b, impact, fake = stack
    a.post("/api/network", json={"mode": "slow"})
    r = up(a, photos / "gosaba-embankment_before.jpg")  # routine: no note, no status
    assert r["priority"] < 65
    res = a.post("/api/sync/now").json()
    assert res["push"]["vectors"] == 1 and res["push"]["deferred"] == 1 and res["push"]["media"] == 0
    a.post("/api/network", json={"mode": "online"})
    a.app.state.engine.link.reset()
    res = a.post("/api/sync/now").json()
    assert res["push"]["media"] == 1


def test_device_edit_is_retried_until_impact_accepts_it(stack, photos):
    a, b, impact, fake = stack
    r = up(a, photos / "pond-a_before.jpg", note="plastic floating")
    a.post("/api/sync/now")
    eid = r["id"] if "id" in r else r["evidence_id"]
    settings = impact.app.state.service.s
    settings.ingest_token = "rotated"  # Impact refuses this device for now
    a.patch(f"/api/evidence/{eid}", json={"note": "plastic removed", "consent": True})
    a.post("/api/sync/now")
    assert impact.get(f"/api/assets/{eid}").json()["asset"]["note"] == "plastic floating"
    assert a.get("/api/outbox").json()["counts"].get("failed") == 1  # kept, not dropped
    settings.ingest_token = "tok"
    a.post("/api/sync/now")
    asset = impact.get(f"/api/assets/{eid}").json()["asset"]
    assert asset["note"] == "plastic removed" and asset["consent"] == 1
    assert not a.get("/api/outbox").json()["counts"].get("failed")


def test_colleague_photos_from_a_snapshot_never_show_a_broken_image(stack, photos):
    a, b, impact, fake = stack
    r = up(a, photos / "pond-a_after.jpg", note="pond after clean-up")
    a.post("/api/sync/now")
    assert b.post("/api/sync/now").json()["pull"]["mode"] == "full"  # arrives in a snapshot, not point by point
    eid = r["id"] if "id" in r else r["evidence_id"]
    for offline in (False, True):
        b.post("/api/network", json={"offline": offline})
        t = b.get(f"/media/thumb/{eid}")
        assert t.status_code == 200 and t.headers["content-type"].split(";")[0] in ("image/jpeg", "image/svg+xml")
    b.post("/api/network", json={"offline": False})


def test_slow_cloud_processing_is_not_mistaken_for_a_slow_link(stack, photos):
    a, b, impact, fake = stack
    upload = fake.upload

    def slow_upload(*args, **kwargs):  # Cloudinary taking its time, on a fast local link
        time.sleep(1.5)
        return upload(*args, **kwargs)

    fake.upload = slow_upload
    up(a, photos / "pond-a_before.jpg", note="pond before")
    assert a.post("/api/sync/now").json()["push"]["media"] == 1
    link = a.get("/api/system").json()["sync"]["link"]
    assert link["quality"] == "good", link  # routine photos must not be held back as if on 2G


@pytest.fixture()
def regional(tmp_path, qdrant_cluster_url, qdrant_api_key):
    collection = f"saakshi_regional_{uuid.uuid4().hex[:8]}"
    fake = FakeCloudinary()
    settings = ImpactSettings(data_dir=tmp_path / "impact", qdrant_url=qdrant_cluster_url, qdrant_api_key=qdrant_api_key, collection=collection, ingest_token="tok")
    svc = ImpactService(settings, store=fake)
    app = impact_app(settings, svc)
    port = 19000 + (uuid.uuid4().int % 1000)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):
        try:
            httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=0.5)
            break
        except Exception:
            time.sleep(0.1)

    def device(dev):
        return TestClient(field_app(FieldSettings(device_id=dev, device_name=dev, data_root=tmp_path / "field", qdrant_url=qdrant_cluster_url, qdrant_api_key=qdrant_api_key,
                                                  collection=collection, impact_url=f"http://127.0.0.1:{port}", ingest_token="tok"),
                                    start_sync=False))

    with device("dev-a") as a, device("dev-b") as b:
        yield a, b, TestClient(app)
    server.should_exit = True
    try:
        httpx.delete(f"{qdrant_cluster_url}/collections/{collection}", headers={"api-key": qdrant_api_key} if qdrant_api_key else {}, timeout=15)
    except Exception:
        pass


def test_impact_search_works_before_the_first_photo(regional, photos):
    a, b, impact = regional
    # a fresh regional collection has no shard key yet; Qdrant drops queries on it
    r = impact.get("/api/search", params={"q": "embankment"})
    assert r.status_code == 200 and r.json()["items"] == []
    up(a, photos / "gosaba-embankment_before.jpg", note="bare embankment")
    a.post("/api/sync/now")  # the device creates the region's shard key
    assert impact.get("/api/search", params={"q": "embankment"}).json()["items"]


def test_regional_layout_devices_mirror_only_their_regions(regional, photos):
    a, b, impact = regional
    up(a, photos / "gosaba-embankment_before.jpg", note="bare embankment in the Sundarbans")
    up(a, photos / "park-street-corner_before.jpg", note="garbage at Park Street")
    r = a.post("/api/sync/now").json()
    assert r["pull"]["layout"] == "regional"
    # B serves only the mangrove project
    b.post("/api/regions", json={"regions": ["mangrove-gosaba"]})
    r = b.post("/api/sync/now").json()
    assert list(r["pull"]["regions"]) == ["mangrove-gosaba"]
    mirrors = b.get("/api/regions").json()["mirrors"]
    assert set(mirrors) == {"mangrove-gosaba"} and mirrors["mangrove-gosaba"] == 1
    assert b.get("/api/search", params={"q": "Sundarbans embankment"}).json()["results"]
    assert not b.get("/api/search", params={"q": "Park Street garbage"}).json()["results"]
    # A receives HQ verdicts for photos in both regions
    a.post("/api/sync/now")
    mine = a.get("/api/evidence", params={"include_mirror": False}).json()["items"]
    assert all((i.get("cloud") or {}).get("integrity_level") for i in mine)


def test_pull_picks_delta_or_snapshot(stack, photos, monkeypatch):
    """A few changed points travel one by one; many changes, or a due reconcile, use a partial snapshot."""
    from field.app import sync as sync_mod

    a, b, impact, fake = stack
    up(a, photos / "park-street-corner_before.jpg", note="garbage at the corner")
    a.post("/api/sync/now")
    b.post("/api/sync/now")                      # B's first pull: full snapshot of the region
    # every photo here is in the same region (Clean Streets), so both server layouts behave alike
    up(a, photos / "park-street-corner_after.jpg", note="corner swept clean")
    a.post("/api/sync/now")

    r = b.post("/api/sync/now").json()["pull"]
    region = next(iter(r["regions"].values()))
    assert r["mode"] == "delta" and region["copied"] >= 1
    assert 0 < r["bytes"] < 200_000               # a few KB per point, not a segment
    hits = b.get("/api/search", params={"q": "corner swept clean"}).json()["results"]
    assert any(h["note"] == "corner swept clean" for h in hits)

    # nothing new: no transfer at all, and the pull says so
    r = b.post("/api/sync/now").json()["pull"]
    assert r["bytes"] == 0 and r["mode"] == "none"

    # more changes than the delta limit: partial snapshot, and the delta shard is folded in
    monkeypatch.setattr(sync_mod, "DELTA_MAX_POINTS", 0)
    up(a, photos / "gariahat-drain_before.jpg", note="drain blocked again")
    a.post("/api/sync/now")
    r = b.post("/api/sync/now").json()["pull"]
    assert r["mode"] == "partial"
    assert b.get("/api/system").json()["device"]["memory"]["delta_points"] == {}
