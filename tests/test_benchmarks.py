"""The benchmark code is part of the argument, so it is tested like the product."""

from bench import bench_fieldday as fd
from bench.report import headlines
from field.app.policy import PolicySettings


def test_field_day_is_reproducible_and_fair():
    """All three approaches face the same day: same captures, same queries, same network."""
    sc = fd.SCENARIOS["rural"]
    a, b = fd.make_day(7, sc), fd.make_day(7, sc)
    assert [c.photo_b for c in a.captures] == [c.photo_b for c in b.captures]
    assert a.links == b.links
    rows = {s: fd.summarize(a, s, fd.simulate(a, s, 434.0, PolicySettings(), 3.0), 5.0) for s in fd.STRATEGIES}
    assert len({r["captures"] for r in rows.values()}) == 1


def test_saakshi_policy_properties_hold_in_the_simulation():
    out = fd.run(days=3)
    for sc in out["scenarios"].values():
        s, cloud = sc["saakshi"], sc["cloud"]
        assert s["faces_unblurred_per_day"] == 0              # faces only leave blurred
        assert s["dup_uploads_per_day"] == 0                  # repeat shots are linked, not re-sent
        assert s["query_answered_pct"] == 100.0               # search never needs the network
        assert cloud["faces_unblurred_per_day"] > 0
        assert s["mb_up_slow_per_day"] <= cloud["mb_up_slow_per_day"]
    remote = out["scenarios"]["remote"]
    assert remote["saakshi"]["team_recall_pct"] > remote["cloud"]["team_recall_pct"]
    assert remote["saakshi"]["urgent_photo_min_p50"] <= remote["cloud"]["urgent_photo_min_p50"]


def test_headlines_are_computed_from_results():
    search = {"sizes": [{"n": 1000, "edge": {"p50_ms": 0.2, "recall_at_10": 1.0}, "numpy": {"p50_ms": 0.1}},
                        {"n": 50000, "edge": {"p50_ms": 1.5, "recall_at_10": 0.99}, "numpy": {"p50_ms": 6.0},
                         "server_http": {"p50_ms": 5.0}}]}
    sync = {"regional": {"projects": 5, "first_pull": {"bytes": 7_000_000}, "one_point": {"delta": {"bytes": 3000}, "partial": {"bytes": 430_000}}},
            "global": {"first_pull": {"bytes": 29_000_000}}}
    h = headlines(search, sync, None)
    assert h[0]["value"] == "1.5 ms" and "50,000" in h[0]["label"]
    assert h[1]["value"] == "3 KB"
    assert h[2]["value"] == "7.0 MB"
