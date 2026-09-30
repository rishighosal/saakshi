"""Impact: reading the dashboard is open; changing evidence or spending Cloudinary quota needs the token."""

import pytest
from fastapi.testclient import TestClient

from impact.app.main import create_app
from impact.app.service import ImpactService, ImpactSettings
from tests.fake_cloud import FakeCloudinary

PROJECT = {"name": "Lake Cleanup", "lat": 22.5, "lon": 88.4, "radius_m": 3000, "start_date": "2026-01-01", "end_date": "2026-12-31"}


def _client(tmp_path, token):
    settings = ImpactSettings(data_dir=tmp_path, ingest_token=token)
    return TestClient(create_app(settings, ImpactService(settings, store=FakeCloudinary())))


def _writes(photo: bytes):
    return [
        ("post", "/api/projects", {"json": PROJECT}),
        ("patch", "/api/sites/pond-a", {"json": {"name": "Pond A (north)"}}),
        ("patch", "/api/evidence/some-id", {"json": {"note": "edited"}}),
        ("post", "/api/upload", {"files": [("files", ("p.jpg", photo, "image/jpeg"))]}),
        ("post", "/api/pairs", {"json": {"before_id": "a", "after_id": "b"}}),
        ("post", "/api/pairs/1/describe", {}),
        ("post", "/api/campaign", {"json": {"caption": "hi", "asset_id": "some-id"}}),
        ("post", "/api/admin/rebuild", {}),
    ]


@pytest.mark.parametrize("header", [None, "wrong"])
def test_every_write_needs_the_token(tmp_path, photos, header):
    photo = (photos / "pond-a_before.jpg").read_bytes()
    with _client(tmp_path, "tok") as c:
        headers = {"X-Saakshi-Token": header} if header else {}
        for method, path, kw in _writes(photo):
            r = getattr(c, method)(path, headers=headers, **kw)
            assert r.status_code == 401, (method, path, r.status_code)
        assert c.get("/api/assets").json()["items"] == []  # nothing was uploaded


def test_reads_are_open_and_the_token_unlocks_writes(tmp_path, photos):
    with _client(tmp_path, "tok") as c:
        for path in ("/api/status", "/api/overview", "/api/projects", "/api/assets", "/api/events"):
            assert c.get(path).status_code == 200, path
        ok = {"X-Saakshi-Token": "tok"}
        r = c.post("/api/upload", headers=ok, files=[("files", ("p.jpg", (photos / "pond-a_before.jpg").read_bytes(), "image/jpeg"))])
        asset = r.json()["results"][0]
        assert asset["project_id"] == "pond-revival-south-kolkata"
        e = c.patch(f"/api/evidence/{asset['id']}", headers=ok, json={"note": "silt removed"})
        assert e.status_code == 200 and e.json()["note"] == "silt removed"


def test_without_a_configured_token_writes_stay_open(tmp_path):
    with _client(tmp_path, None) as c:
        assert c.post("/api/projects", json=PROJECT).status_code == 200


def test_rebuild_reads_only_this_deployments_cloudinary_folder(tmp_path, monkeypatch):
    from impact.app.cloud import CloudinaryStore

    store = CloudinaryStore("demo", "k", "s", folder="saakshi-dev")
    asked = []
    monkeypatch.setattr(store, "search_all", lambda expression, max_results=500: asked.append(expression) or [])
    svc = ImpactService(ImpactSettings(data_dir=tmp_path, folder="saakshi-dev"), store=store)
    svc.rebuild_from_cloudinary()
    assert asked == ['tags=saakshi AND public_id:"saakshi-dev/*"']


def test_public_demo_is_announced_and_pairs_carry_their_campaign_images(tmp_path, photos):
    settings = ImpactSettings(data_dir=tmp_path, ingest_token="tok", public_demo=True,
                              demo_video_url="https://example.org/video", repo_url="https://example.org/repo")
    svc = ImpactService(settings, store=FakeCloudinary())
    with TestClient(create_app(settings, svc)) as c:
        demo = c.get("/api/status").json()["public_demo"]
        assert demo == {"video_url": "https://example.org/video", "repo_url": "https://example.org/repo"}
        ok = {"X-Saakshi-Token": "tok"}
        for name in ("park-street-corner_before.jpg", "park-street-corner_after.jpg"):
            c.post("/api/upload", headers=ok, files=[("files", (name, (photos / name).read_bytes(), "image/jpeg"))])
        pair = c.get("/api/projects/clean-streets-kolkata").json()["pairs"][0]
        assert pair["campaign"] == []
        c.post("/api/campaign", headers=ok, json={"caption": "Corner cleared", "pair_id": pair["id"]})
        pair = c.get("/api/projects/clean-streets-kolkata").json()["pairs"][0]
        # two before/after posts plus the four social formats of the "after" photo
        assert len(pair["campaign"]) == 6 and all(v["url"].startswith("https://") and v["steps"] for v in pair["campaign"])


def test_public_demo_is_off_by_default(tmp_path):
    with _client(tmp_path, "tok") as c:
        assert c.get("/api/status").json()["public_demo"] is None


def test_rebuild_keeps_faces_blurred_checks_and_paid_ai_text(tmp_path, photos, monkeypatch):
    """A redeploy (Render wipes its disk) rebuilds from Cloudinary: public pages must still blur faces."""
    from impact.app.cloud import CloudinaryStore

    class WithFaces(FakeCloudinary):
        def upload(self, *args, **kwargs):
            return {**super().upload(*args, **kwargs), "faces": [[10, 10, 40, 40]]}

    fake = WithFaces()
    first = ImpactService(ImpactSettings(data_dir=tmp_path / "before"), store=fake)
    for name in ("park-street-corner_before.jpg", "park-street-corner_after.jpg"):
        first.ingest((photos / name).read_bytes(), name, {"note": name}, "web")
    pair = first.db.pairs()[0]
    first.describe_change(pair["id"])
    first.campaign("Corner cleared", pair_id=pair["id"])
    before = {a["id"]: a for a in first.db.assets()}

    store = CloudinaryStore("demo-cloud", "k", "s", folder="saakshi")
    found = [{**fake.uploads[pid], "resource_type": "image", "context": {"custom": ctx}} for pid, ctx in fake.context.items()]
    monkeypatch.setattr(store, "search_all", lambda expression, max_results=500: found)
    again = ImpactService(ImpactSettings(data_dir=tmp_path / "after"), store=store)
    assert again.rebuild_from_cloudinary() == {"restored": 2}
    for a in again.db.assets():
        was = before[a["id"]]
        assert a["faces"] == 1 and "blur_faces" in again.present(a)["public_thumb_url"]
        assert a["integrity"]["checks"] == was["integrity"]["checks"] and a["integrity_score"] == was["integrity_score"]
        assert a["has_exif"] == was["has_exif"] and a["dhash"] == was["dhash"]
    assert again.db.pairs()[0]["change_text"] == first.db.pair(pair["id"])["change_text"]
    assert len(again.present_pair(again.db.pairs()[0])["campaign"]) == 6


def test_a_flagged_photo_is_never_a_projects_cover_when_others_exist(tmp_path, photos):
    with _client(tmp_path, None) as c:
        def up(name, **form):
            return c.post("/api/upload", data=form, files=[("files", (name, (photos / name).read_bytes(), "image/jpeg"))]).json()

        up("pond-a_after.jpg")  # the original, in the pond project
        assert up("no-gps_saplings.jpg", project_id="clean-streets-kolkata")["results"][0]["integrity_level"] == "review"
        flagged = up("reused_pond-photo.jpg", project_id="clean-streets-kolkata")["results"][0]  # the pond photo again: reuse
        assert flagged["integrity_level"] == "flagged"
        cover = next(p for p in c.get("/api/overview").json()["projects"] if p["id"] == "clean-streets-kolkata")["cover"]
        assert cover and flagged["id"] not in cover
