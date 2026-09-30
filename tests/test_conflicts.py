from field.app.conflicts import Claim, evaluate
from field.app.merge import merge_payloads

H = 3600.0


def c(status, ts, dev="dev-a", eid=None):
    return Claim("site-1", status, ts, dev, eid or f"{dev}-{status}-{ts}")


def test_first_claim_is_new():
    assert evaluate(None, c("completed", 0)).kind == "new"


def test_agreement_is_not_a_conflict():
    assert evaluate(c("completed", 0), c("completed", 1 * H, "dev-b")).kind == "same"


def test_close_disagreement_between_devices_is_a_conflict():
    out = evaluate(c("completed", 0), c("damaged", 2 * H, "dev-b"))
    assert out.kind == "conflict"
    assert "dev-a" in out.message and "dev-b" in out.message


def test_newer_evidence_wins_outside_window():
    assert evaluate(c("in_progress", 0), c("completed", 30 * H, "dev-b")).kind == "newer_wins"


def test_older_evidence_is_ignored():
    assert evaluate(c("completed", 30 * H), c("in_progress", 0, "dev-b")).kind == "older_ignored"


def test_same_device_changing_its_mind_is_not_a_conflict():
    assert evaluate(c("in_progress", 0), c("completed", 1 * H)).kind == "newer_wins"


def test_merge_takes_newer_field_values_only():
    local = {"note": "local note", "status_claim": "completed", "field_ts": {"note": 10, "status_claim": 50}, "field_by": {}, "version": 2}
    remote = {"note": "remote note", "status_claim": "damaged", "field_ts": {"note": 20, "status_claim": 40}, "field_by": {"note": "dev-b"}, "version": 3}
    merged, taken = merge_payloads(local, remote)
    assert taken == ["note"]
    assert merged["note"] == "remote note"
    assert merged["status_claim"] == "completed"
    assert merged["field_by"]["note"] == "dev-b"
    assert merged["version"] == 4


def test_merge_copies_cloud_enrichment():
    merged, taken = merge_payloads({"field_ts": {}}, {"field_ts": {}, "cloud": {"integrity_level": "verified"}})
    assert merged["cloud"]["integrity_level"] == "verified"
    assert taken == []
