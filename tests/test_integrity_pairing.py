from impact.app.integrity import IntegrityInput, ReuseMatch, score
from impact.app.pairing import PairCandidate, best_pair, describe, tag_delta, verdict
from saakshi_core.projects import load_bundled, parse_projects

PROJECTS = {p.id: p for p in parse_projects(load_bundled())}
STREETS = PROJECTS["clean-streets-kolkata"]


def base(**kw):
    d = dict(has_exif=True, has_gps=True, lat=22.5530, lon=88.3520, captured_at="2026-09-20T10:00:00",
             uploaded_at="2026-09-21T10:00:00+00:00", edited_with=None, quality=0.8, faces=0, consent=False,
             project=STREETS, site_id="s1", reuse=[])
    d.update(kw)
    return IntegrityInput(**d)


def keys(result, status):
    return {c.key for c in result.checks if c.status == status}


def test_clean_photo_is_verified():
    r = score(base())
    assert r.level == "verified" and r.score == 100


def test_photo_outside_project_area_is_flagged():
    r = score(base(lat=28.61, lon=77.21))  # Delhi
    assert "in_area" in keys(r, "fail")
    assert r.level == "flagged"


def test_photo_before_project_start_fails():
    r = score(base(captured_at="2025-01-05T09:00:00"))
    assert "in_window" in keys(r, "fail")


def test_reuse_from_another_project_is_flagged():
    m = ReuseMatch("x", "pond-revival-south-kolkata", "Pond Revival", "pond-a", "2026-09-01T00:00:00", "near-identical image", 2)
    r = score(base(reuse=[m]))
    assert "reuse" in keys(r, "fail")
    assert r.level == "flagged"
    assert any("Pond Revival" in c.message for c in r.checks)


def test_repeat_shot_of_same_site_is_fine():
    m = ReuseMatch("x", STREETS.id, STREETS.name, "s1", None, "near-identical image", 3)
    r = score(base(reuse=[m]))
    assert r.level == "verified"


def test_stripped_metadata_needs_review():
    r = score(base(has_exif=False, has_gps=False, lat=None, lon=None, captured_at=None))
    assert r.level == "review"
    assert {"exif", "gps"} <= keys(r, "warn")


def test_edited_and_blurry_photo_warns():
    r = score(base(edited_with="Snapseed 2.0", quality=0.2))
    assert {"edited", "quality"} <= keys(r, "warn")


def test_pairing_picks_earliest_and_best_matching_late_photo():
    items = [
        PairCandidate("a", 0, ["waste_present"], [1, 0, 0]),
        PairCandidate("b", 1 * 3600, ["waste_present"], [0.9, 0.1, 0]),
        PairCandidate("c", 48 * 3600, ["clean_area"], [0.95, 0.05, 0]),
        PairCandidate("d", 49 * 3600, ["clean_area"], [0, 1, 0]),
    ]
    p = best_pair(items, min_gap_hours=1)
    assert p.before_id == "a" and p.after_id == "c"
    assert p.verdict == "improved"
    assert p.removed == ["waste_present"] and p.added == ["clean_area"]


def test_pairing_skips_flagged_and_needs_time_gap():
    items = [PairCandidate("a", 0, [], None), PairCandidate("b", 600, [], None)]
    assert best_pair(items, min_gap_hours=1) is None
    items = [PairCandidate("a", 0, [], None), PairCandidate("b", 7200, [], None, level="flagged")]
    assert best_pair(items) is None


def test_verdict_and_description():
    d = tag_delta(["waste_present", "water_body"], ["water_body", "sapling_planting"])
    assert verdict(d["added"], d["removed"]) == "improved"
    assert verdict(["damage"], []) == "declined"
    assert "no longer shows waste present" in describe(d["added"], d["removed"], "improved")
