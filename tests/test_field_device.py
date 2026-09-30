"""Device app with no network at all: capture, search, policy, edits."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from field.app.main import create_app
from field.app.service import FieldSettings


@pytest.fixture()
def device(tmp_path):
    app = create_app(FieldSettings(device_id="dev-t", device_name="Test", data_root=tmp_path), start_sync=False)
    with TestClient(app) as c:
        yield c


def upload(c, path: Path, **form):
    r = c.post("/api/evidence", files=[("files", (path.name, path.read_bytes(), "image/jpeg"))], data=form)
    assert r.status_code == 200
    return r.json()["results"][0]


def test_capture_places_photo_on_project_and_site(device, photos):
    r = upload(device, photos / "gosaba-embankment_before.jpg", note="bare embankment")
    assert r["project_id"] == "mangrove-gosaba"
    assert r["site_id"] == "gosaba-embankment-north"
    assert r["sync_state"] == "queued"


def test_offline_search_finds_notes_fast(device, photos):
    upload(device, photos / "park-street-corner_before.jpg", note="garbage pile blocking the drain")
    upload(device, photos / "pond-a_before.jpg", note="plastic floating on pond")
    r = device.get("/api/search", params={"q": "drain garbage"}).json()
    assert r["results"][0]["note"] == "garbage pile blocking the drain"
    assert r["latency_ms"] < 50


def test_private_items_stay_local(device, photos):
    r = upload(device, photos / "no-gps_saplings.jpg", private="true")
    assert r["sync_action"] == "local_only"
    assert device.get("/api/outbox").json()["items"] == []


def test_same_file_twice_is_not_duplicated(device, photos):
    upload(device, photos / "pond-a_after.jpg")
    r = upload(device, photos / "pond-a_after.jpg")
    assert r.get("duplicate_file") is True
    assert device.get("/api/status").json()["memory"]["local_points"] == 1


def test_edit_bumps_version_and_is_searchable(device, photos):
    r = upload(device, photos / "pond-a_before.jpg", note="pond")
    e = device.patch(f"/api/evidence/{r['id'] if 'id' in r else r['evidence_id']}", json={"note": "silt removed from the pond"}).json()
    assert e["version"] == 2
    hits = device.get("/api/search", params={"q": "silt"}).json()["results"]
    assert hits and hits[0]["note"] == "silt removed from the pond"


def test_network_switch_and_sync_now_offline(device):
    device.post("/api/network", json={"offline": True})
    s = device.get("/api/status").json()
    assert s["sync"]["simulated_offline"] is True
    assert device.post("/api/sync/now").json()["ok"] is False


def test_conflicting_local_claims_on_same_site(device, photos):
    upload(device, photos / "pond-a_before.jpg", status_claim="in_progress")
    r = upload(device, photos / "pond-a_after.jpg", status_claim="completed")
    assert r["claim"]["kind"] in ("newer_wins", "new", "same")  # same device: never a conflict
    sites = device.get("/api/conflicts").json()["sites"]
    assert sites[0]["status"] == "completed"
