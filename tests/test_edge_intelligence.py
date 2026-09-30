"""Edge intelligence without any network: search modes, assistant, storage budget, timelines."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from field.app.main import create_app
from field.app.service import FieldSettings
from field.app.sync import LinkMonitor


@pytest.fixture()
def device(tmp_path):
    app = create_app(FieldSettings(device_id="dev-t", device_name="Test", data_root=tmp_path), start_sync=False)
    with TestClient(app) as c:
        yield c


def up(c, path: Path, **form):
    r = c.post("/api/evidence", files=[("files", (path.name, path.read_bytes(), "image/jpeg"))], data=form)
    return r.json()["results"][0]


@pytest.fixture()
def loaded(device, photos):
    up(device, photos / "park-street-corner_before.jpg", note="garbage pile blocking the drain", status_claim="needs_attention")
    up(device, photos / "park-street-corner_after.jpg", note="drain cleared by volunteers", status_claim="completed")
    up(device, photos / "pond-a_before.jpg", note="plastic floating on the pond")
    up(device, photos / "gosaba-embankment_before.jpg", note="bare embankment, saplings planted", status_claim="in_progress")
    return device


def test_search_returns_query_plan_and_reasons(loaded):
    r = loaded.get("/api/search", params={"q": "drain garbage"}).json()
    assert r["results"][0]["note"] == "garbage pile blocking the drain"
    assert r["plan"] and "BM25" in r["plan"][0]
    assert "garbage" in r["results"][0]["why"]["words"]


def test_recency_boost_changes_the_plan(loaded):
    r = loaded.get("/api/search", params={"q": "drain", "recency_hours": 24}).json()
    assert any("recency" in step for step in r["plan"])
    assert r["results"]


def test_geo_radius_filter_keeps_only_nearby_evidence(loaded):
    # 2 km around Pond A excludes Park Street (8 km away) and Gosaba
    r = loaded.get("/api/search", params={"q": "garbage drain plastic pond embankment", "near_site": "pond-a", "radius_km": 2}).json()
    assert r["results"] and all(i["site_id"] == "pond-a" for i in r["results"])
    assert any("within 2.0 km" in s for s in r["plan"])


def test_filters_only_browsing(loaded):
    r = loaded.get("/api/search", params={"status": "completed"}).json()
    assert [i["note"] for i in r["results"]] == ["drain cleared by volunteers"]


def test_assistant_site_briefing_with_citations(loaded):
    a = loaded.post("/api/ask", json={"question": "What is the status of Embankment North?"}).json()
    assert a["intent"] == "site"
    assert "in progress" in a["answer"]
    assert a["citations"] and a["offline"] is True


def test_assistant_outbox_and_search_intents(loaded):
    a = loaded.post("/api/ask", json={"question": "What is still waiting to sync?"}).json()
    assert a["intent"] == "outbox" and "waiting to sync" in a["answer"]
    b = loaded.post("/api/ask", json={"question": "plastic in water"}).json()
    assert b["intent"] == "search" and b["citations"][0]["label"] == "plastic floating on the pond"


def test_site_timeline_shows_evolving_knowledge(loaded):
    sites = loaded.get("/api/sites").json()["items"]
    street = next(s for s in sites if s["evidence"] == 2)
    tl = loaded.get(f"/api/sites/{street['site_id']}/timeline").json()
    statuses = [e["status"] for e in tl["events"] if e["type"] == "photo"]
    assert statuses == ["needs_attention", "completed"]
    assert tl["current"]["status"] == "completed"


def test_storage_budget_only_releases_originals_safe_in_the_cloud(loaded):
    svc = loaded.app.state.service
    items = svc.list_evidence(include_mirror=False)
    # Pretend two items are synced and checked by HQ
    for it in items[:2]:
        svc.memory.set_payload(it["id"], {"sync_state": "synced", "cloud": {"public_id": "x", "integrity_level": "verified"}})
    released = loaded.post("/api/settings", json={"media_budget_mb": 0.001}).json()["storage"]["originals_released"]
    assert released == 2
    evicted = [i for i in svc.list_evidence(include_mirror=False) if i.get("media_evicted")]
    assert {i["id"] for i in evicted} == {i["id"] for i in items[:2]}
    assert loaded.get("/api/search", params={"q": evicted[0]["note"]}).json()["results"]  # still searchable
    assert loaded.get(f"/media/original/{evicted[0]['id']}").status_code == 410


def test_more_like_this(loaded):
    items = loaded.get("/api/evidence").json()["items"]
    r = loaded.get(f"/api/evidence/{items[0]['id']}/more-like-this", params={"exclude": items[1]["id"]}).json()["items"]
    assert r and all(i["id"] not in (items[0]["id"], items[1]["id"]) for i in r)


def test_link_monitor_classifies_links():
    m = LinkMonitor()
    assert m.quality(True) == "unknown" and m.quality(False) == "offline"
    m.record(2 * 1024, 0.5)  # tiny transfers measure latency, not bandwidth: ignored
    assert m.quality(True) == "unknown"
    m.record(48 * 1024, 2.0)
    assert m.quality(True) == "constrained"
    m.reset()
    m.record(2 * 1024 * 1024, 1.0)
    assert m.quality(True) == "good"


def test_network_modes(device):
    for mode in ("slow", "offline", "online"):
        assert device.post("/api/network", json={"mode": mode}).json()["network_mode"] == mode
    assert device.post("/api/network", json={"mode": "4g"}).status_code == 400


def test_live_benchmark_runs_on_the_device(loaded):
    r = loaded.post("/api/benchmark/live").json()
    assert r["device"]["works_offline"] and r["device"]["points"] >= 4
    assert r["device"]["p50_ms"] > 0
    assert r["server"]["reachable"] is False


def test_a_slow_server_is_reachable_not_offline(tmp_path):
    """A first HTTPS handshake over 2G (or to a far cloud region) can take seconds; that is not offline."""
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Slow(BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(2.5)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            return None

    srv = HTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        app = create_app(FieldSettings(device_id="dev-t", device_name="Test", data_root=tmp_path,
                                       qdrant_url=f"http://127.0.0.1:{srv.server_port}"), start_sync=False)
        with TestClient(app) as c:
            assert c.app.state.engine.reachability()["qdrant"] is True
    finally:
        srv.shutdown()
