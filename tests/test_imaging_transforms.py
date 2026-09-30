import cloudinary
from PIL import Image

from impact.app import transforms
from saakshi_core.imaging import dhash, hamming_hex, read_exif
from saakshi_core.projects import assign_site, guess_project, load_bundled, parse_projects

cloudinary.config(cloud_name="demo", api_key="k", api_secret="s", secure=True)


def test_exif_gps_and_time_are_read(photos):
    e = read_exif(photos / "pond-a_before.jpg")
    assert e.has_exif and e.has_gps
    assert abs(e.lat - 22.4871) < 1e-3 and abs(e.lon - 88.3791) < 1e-3
    assert e.captured_at and e.captured_at[4] == "-"


def test_photo_without_gps(photos):
    e = read_exif(photos / "no-gps_saplings.jpg")
    assert not e.has_gps


def test_dhash_survives_resize_and_recompression(photos):
    img = Image.open(photos / "pond-a_after.jpg")
    small = img.resize((400, 300))
    assert hamming_hex(dhash(img), dhash(small)) <= 4
    other = Image.open(photos / "gosaba-embankment_before.jpg")
    assert hamming_hex(dhash(img), dhash(other)) > 10


def test_projects_and_site_assignment():
    projects = parse_projects(load_bundled())
    p = guess_project(projects, 22.4870, 88.3790)
    assert p.id == "pond-revival-south-kolkata"  # smallest area wins over city-wide project
    s = assign_site(p, 22.48705, 88.37905)
    assert s.id == "pond-a"
    s2 = assign_site(p, 22.5000, 88.3700)
    assert s2.auto and s2.id.startswith("site_")


def test_before_after_composite_uses_layer_ids():
    url, t = transforms.before_after("saakshi/a", "saakshi/b", "BEFORE 1 Sep", "AFTER 9 Sep")
    assert "l_saakshi:b" in url
    assert "e_blur_faces" in t
    steps = transforms.explain(t)
    assert steps[0].startswith("Crop to fill 800×600")
    assert any("Overlay photo saakshi/b" in s for s in steps)
    assert any("Blur every detected face" in s for s in steps)


def test_campaign_variant_caption_and_download():
    url, t = transforms.campaign_variant("saakshi/a", transforms.FORMATS[2], "Pond cleaned, 30 kg removed", "Demo NGO")
    assert "c_fill,g_auto,h_1920,w_1080" in url
    assert any('Add text "Pond cleaned, 30 kg removed"' == s for s in transforms.explain(t))
    assert "/fl_attachment:story/" in transforms.attachment(url, "story")


def test_video_frame_url():
    url = transforms.video_frame("saakshi/v1")
    assert "/video/upload/" in url and "so_auto" in url and url.endswith(".jpg")


def test_ai_vision_tagging_respects_the_api_limits(monkeypatch):
    import re

    from impact.app.cloud import MAX_TAG_DEFINITIONS, CloudinaryStore
    from saakshi_core import taxonomy

    store = CloudinaryStore("demo", "k", "s")
    sent = []

    def fake_analyze(mode, body):
        defs = body["tag_definitions"]
        sent.append(len(defs))
        assert all(re.fullmatch(r"[a-z0-9-]+", d["name"]) for d in defs)  # the API's rule for tag names
        return {"data": {"analysis": {"tags": [{"name": d["name"]} for d in defs if d["name"] in ("water-body", "waste-present")]}}}

    monkeypatch.setattr(store, "_analyze", fake_analyze)
    tags = store.ai_tags("https://example.org/p.jpg")
    assert all(n <= MAX_TAG_DEFINITIONS for n in sent)
    assert sum(sent) == len(taxonomy.TAGS)
    assert tags == ["waste_present", "water_body"]


def test_ai_vision_requests_for_one_photo_run_together(tmp_path, monkeypatch):
    """Tag batches and the caption go out in parallel: one round-trip, not three."""
    import time

    from impact.app.cloud import CloudinaryStore
    from impact.app.service import ImpactService, ImpactSettings

    store = CloudinaryStore("demo", "k", "s")
    monkeypatch.setattr(store, "_analyze", lambda mode, body: (time.sleep(0.4), {"data": {"analysis": {"tags": [], "responses": [{"value": "A pond."}]}}})[1])
    svc = ImpactService(ImpactSettings(data_dir=tmp_path), store=store)
    t0 = time.perf_counter()
    tags, caption, source = svc._understand("https://res.cloudinary.com/demo/image/upload/v1/saakshi/x.jpg", "saakshi/x", None)
    took = time.perf_counter() - t0
    assert caption == "A pond." and source == "cloudinary-ai-vision"
    assert took < 0.9, f"{took:.2f}s: requests ran one after another"


def _big_noisy_jpeg(photos, side=3000):
    import io

    import numpy as np

    src = Image.open(photos / "gosaba-embankment_after.jpg")
    big = src.resize((side, side * 3 // 4))
    noise = np.random.default_rng(3).integers(-20, 20, (side * 3 // 4, side, 3))
    big = Image.fromarray(np.clip(np.asarray(big).astype(np.int16) + noise, 0, 255).astype(np.uint8))
    buf = io.BytesIO()
    big.save(buf, "JPEG", quality=95, exif=src.info["exif"])
    return buf.getvalue()


def test_oversized_photo_is_shrunk_under_the_limit_keeping_gps(photos):
    from saakshi_core.imaging import fit_within

    data = _big_noisy_jpeg(photos)
    limit = 1_000_000
    assert len(data) > limit
    out = fit_within(data, limit)
    assert len(out) <= limit
    before, after = read_exif(data), read_exif(out)
    assert after.has_gps and (after.lat, after.lon, after.captured_at) == (before.lat, before.lon, before.captured_at)


def test_impact_stores_a_copy_of_a_photo_over_the_storage_limit(tmp_path, photos):
    from impact.app.service import ImpactService, ImpactSettings
    from saakshi_core.imaging import sha256_bytes
    from tests.fake_cloud import FakeCloudinary

    limit = 1_000_000

    class CappedCloudinary(FakeCloudinary):
        def upload(self, data, *args, **kwargs):
            assert len(data) <= limit, "Cloudinary would refuse this file"
            return super().upload(data, *args, **kwargs)

    data = _big_noisy_jpeg(photos)
    svc = ImpactService(ImpactSettings(data_dir=tmp_path, max_image_bytes=limit), store=CappedCloudinary())
    a = svc.ingest(data, "huge.jpg", {"note": "60 MP phone photo"}, "web")["asset"]
    assert a["has_gps"] and a["sha256"] == sha256_bytes(data)  # the original's hash, for reuse detection
    assert svc.provenance(a["id"])["original"]["original_bytes"] == len(data)
