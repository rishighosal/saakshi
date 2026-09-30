"""A simulated field day: what does each approach deliver when the network comes and goes?

Three approaches face exactly the same day (same officers, same photos, same
network trace, per random seed):

  cloud    a typical cloud app: photos upload first-in-first-out at full
           resolution, and search runs on the server (needs a connection)
  queue    an offline-first form app: the same upload queue, plus a local list
           of the officer's OWN captures; the team's evidence is only
           searchable online
  saakshi  this project: Qdrant Edge on the device with a regional mirror,
           vectors before photos, and the real sync policy
           (field/app/policy.py decide()): urgent first, duplicates linked
           instead of re-uploaded, faces blurred on the device, routine photos
           wait for a better link, compressed copies on a slow link

What is simulated and what is real
  * The policy decisions come from the production function decide(), and the
    deferral / compression thresholds are the ones in field/app/sync.py.
  * The regional snapshot size per pull is read from docs/benchmarks/sync.json
    (measured by bench_sync.py against a real Qdrant server).
  * The day itself (visits, photos, network) is synthetic. Every assumption is
    a named constant below and is written into the results file.

    python -m bench.bench_fieldday --days 30
"""

from __future__ import annotations

import argparse
import json
import math
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np

from bench.common import OUT, machine, save
from field.app.policy import ItemFacts, PolicySettings, decide
from field.app.sync import DEFER_BELOW_PRIORITY, DEFER_MAX_WAIT_S, DELTA_MAX_POINTS, RECONCILE_S

# ------------------------------------------------------------------ assumptions
DAY_MIN = 600                      # 08:00 - 18:00, one-minute steps
OFFICERS = 4                       # field officers working one region
SITES = 8                          # project sites in the region
PHOTOS_PER_VISIT = (4, 9)          # photos taken at a site visit (uniform)
VISIT_MIN = (40, 90)               # time spent at a site
TRAVEL_MIN = (20, 60)              # travel between sites
P_BURST = 0.30                     # a photo is followed by a near-identical retake 1-3 min later
P_URGENT = 0.05                    # note mentions flood / breach / collapse ...
P_BAD_STATUS = 0.10                # site reported damaged / needs attention
P_FACES = 0.20                     # people in the frame, no consent recorded
P_CONSENT = 0.05                   # people in the frame, consent recorded
P_NO_GPS = 0.03
PHOTO_MB = (2.0, 4.5)              # original phone JPEG (12 MP)
COMPRESSED_KB = (220, 380)         # 1600 px, q82 (saakshi_core.imaging.compressed_bytes)
BLURRED_KB = (380, 650)            # 2048 px, q85 face-blurred copy
VECTOR_KB = 4.5                    # 512-d CLIP + BM25 + payload, one Qdrant point
QUERY_RESPONSE_KB = 150            # server search: results + 10 thumbnails
PULL_EVERY_MIN = 5                 # Saakshi mirror refresh interval when online
DELTA_KB_PER_POINT = 3.0           # overridden by docs/benchmarks/sync.json when present
PARTIAL_KB = 434.0                 # partial snapshot floor, overridden by sync.json
QUERY_PATIENCE_S = 15              # a server search slower than this counts as failed
LINK_KBPS = {"good": 250.0, "slow": 24.0, "offline": 0.0}   # uplink, KB/s (~2 Mbit/s 4G, ~190 kbit/s 2G/EDGE)
RTT_S = {"good": 0.08, "slow": 0.9}

# share of time in each link state, and how long a state lasts on average
SCENARIOS: Dict[str, Dict[str, Any]] = {
    "city":   {"mix": {"good": 0.85, "slow": 0.12, "offline": 0.03}, "dwell_min": 25,
               "label": "City ward (Kolkata)"},
    "rural":  {"mix": {"good": 0.35, "slow": 0.40, "offline": 0.25}, "dwell_min": 18,
               "label": "Rural block"},
    "remote": {"mix": {"good": 0.05, "slow": 0.30, "offline": 0.65}, "dwell_min": 30,
               "label": "Remote islands (Sundarbans)"},
}

STRATEGIES = ["cloud", "queue", "saakshi"]


@dataclass
class Capture:
    id: int
    officer: int
    t: int
    site: int
    photo_b: int
    comp_b: int
    blur_b: int
    urgent: bool
    status: Optional[str]
    faces: int
    consent: bool
    has_gps: bool
    novelty: float
    dup_of: Optional[int] = None
    dup_gap_s: float = 0.0


@dataclass
class Query:
    officer: int
    t: int
    site: int


@dataclass
class Day:
    captures: List[Capture]
    queries: List[Query]
    links: List[List[str]]          # links[officer][minute]


# ------------------------------------------------------------------ generation
def make_links(rng: random.Random, scenario: Dict[str, Any]) -> List[str]:
    states = list(scenario["mix"])
    weights = [scenario["mix"][s] for s in states]
    p_switch = 1.0 / scenario["dwell_min"]
    cur = rng.choices(states, weights)[0]
    out = []
    for _ in range(DAY_MIN):
        if rng.random() < p_switch:
            cur = rng.choices(states, weights)[0]
        out.append(cur)
    return out


def make_day(seed: int, scenario: Dict[str, Any]) -> Day:
    rng = random.Random(seed)
    captures: List[Capture] = []
    queries: List[Query] = []
    for o in range(OFFICERS):
        t = rng.randint(0, 30)
        while t < DAY_MIN - 30:
            site = rng.randrange(SITES)
            queries.append(Query(o, t, site))                     # "what does the team already have here?"
            stay = rng.randint(*VISIT_MIN)
            n = rng.randint(*PHOTOS_PER_VISIT)
            for pt in sorted(rng.randint(t, min(DAY_MIN - 1, t + stay)) for _ in range(n)):
                faces = rng.random() < P_FACES + P_CONSENT
                c = Capture(
                    id=len(captures), officer=o, t=pt, site=site,
                    photo_b=int(rng.uniform(*PHOTO_MB) * 1e6), comp_b=int(rng.uniform(*COMPRESSED_KB) * 1e3),
                    blur_b=int(rng.uniform(*BLURRED_KB) * 1e3),
                    urgent=rng.random() < P_URGENT,
                    status=("damaged" if rng.random() < 0.5 else "needs_attention") if rng.random() < P_BAD_STATUS else None,
                    faces=rng.randint(1, 4) if faces else 0,
                    consent=faces and rng.random() < P_CONSENT / (P_FACES + P_CONSENT),
                    has_gps=rng.random() >= P_NO_GPS,
                    novelty=rng.uniform(0.3, 1.0),
                )
                captures.append(c)
                if rng.random() < P_BURST:
                    gap = rng.randint(1, 3)
                    if pt + gap < DAY_MIN:
                        captures.append(Capture(
                            id=len(captures), officer=o, t=pt + gap, site=site, photo_b=c.photo_b, comp_b=c.comp_b, blur_b=c.blur_b,
                            urgent=False, status=None, faces=c.faces, consent=c.consent, has_gps=c.has_gps,
                            novelty=0.03, dup_of=c.id, dup_gap_s=gap * 60.0))
            t += stay + rng.randint(*TRAVEL_MIN)
    captures.sort(key=lambda c: (c.t, c.id))
    links = [make_links(rng, scenario) for _ in range(OFFICERS)]
    return Day(captures, queries, links)


def facts(c: Capture, dup_synced: bool) -> ItemFacts:
    note = "embankment breach, water entering fields" if c.urgent else "routine visit"
    return ItemFacts(faces=c.faces, consent=c.consent, duplicate_of=str(c.dup_of) if c.dup_of is not None else None,
                     duplicate_similarity=0.97 if c.dup_of is not None else 0.0, duplicate_synced=dup_synced,
                     duplicate_gap_s=c.dup_gap_s, new_info=False, novelty=c.novelty, note=note, status_claim=c.status,
                     has_gps=c.has_gps, size_bytes=c.photo_b)


# ------------------------------------------------------------------ simulation
@dataclass
class Upload:
    cap: Capture
    size: int
    kind: str                     # "photo" | "blurred" | "compressed"
    done: int = 0


@dataclass
class Result:
    record_at: Dict[int, int] = field(default_factory=dict)     # capture id -> minute the record is findable on the server
    photo_at: Dict[int, int] = field(default_factory=dict)      # capture id -> minute the photo reached HQ
    bytes_up: float = 0.0
    bytes_up_slow: float = 0.0
    bytes_down: float = 0.0
    wasted: float = 0.0
    faces_unblurred: int = 0
    dup_uploads: int = 0
    dup_bytes: float = 0.0
    skipped_dups: int = 0
    compressed: int = 0
    pull_minutes: Dict[int, List[int]] = field(default_factory=dict)   # officer -> minutes the mirror was refreshed
    delta_pulls: int = 0
    snapshot_pulls: int = 0


def _send(res: Result, up: Upload, budget: float, state: str) -> float:
    """Push bytes of one upload; returns remaining budget."""
    need = up.size - up.done
    use = min(need, budget)
    up.done += use
    res.bytes_up += use
    if state == "slow":
        res.bytes_up_slow += use
    return budget - use


def simulate(day: Day, strategy: str, pull_kb: float, policy: PolicySettings, delta_kb: float = DELTA_KB_PER_POINT) -> Result:
    res = Result()
    by_officer: Dict[int, List[Capture]] = {o: [] for o in range(OFFICERS)}
    for c in day.captures:
        by_officer[c.officer].append(c)
    pending: Dict[int, List[Capture]] = {o: [] for o in range(OFFICERS)}      # waiting for upload
    vec_pending: Dict[int, List[Capture]] = {o: [] for o in range(OFFICERS)}  # saakshi: vectors not yet sent
    current: Dict[int, Optional[Upload]] = dict.fromkeys(range(OFFICERS))
    decisions: Dict[int, Any] = {}
    idx = dict.fromkeys(range(OFFICERS), 0)
    last_seen_change = dict.fromkeys(range(OFFICERS), -1)
    last_reconcile = dict.fromkeys(range(OFFICERS), 0)

    for m in range(DAY_MIN):
        for o in range(OFFICERS):
            caps = by_officer[o]
            while idx[o] < len(caps) and caps[idx[o]].t == m:
                c = caps[idx[o]]
                idx[o] += 1
                if strategy == "saakshi":
                    d = decide(facts(c, c.dup_of is not None and c.dup_of in res.record_at), policy)
                    decisions[c.id] = d
                    if d.action == "skip_duplicate":
                        res.skipped_dups += 1
                        continue
                    vec_pending[o].append(c)
                pending[o].append(c)

            state = day.links[o][m]
            budget = LINK_KBPS[state] * 1024 * 60
            cur = current[o]
            if state == "offline":
                if cur is not None and cur.done:
                    res.wasted += cur.done          # the connection dropped; the upload starts over
                    cur.done = 0
                continue

            if strategy == "saakshi":
                # vectors first: every queued item becomes searchable for the team within the minute
                vec_pending[o].sort(key=lambda c: -decisions[c.id].priority)
                while vec_pending[o] and budget >= VECTOR_KB * 1024:
                    c = vec_pending[o].pop(0)
                    budget -= VECTOR_KB * 1024
                    res.bytes_up += VECTOR_KB * 1024
                    if state == "slow":
                        res.bytes_up_slow += VECTOR_KB * 1024
                    res.record_at[c.id] = m
                # regional mirror refresh: nothing (no change), changed points only, or a partial snapshot
                if m % PULL_EVERY_MIN == 0:
                    since = last_seen_change[o]
                    n = sum(1 for t in res.record_at.values() if since < t <= m)
                    horizon = RECONCILE_S / 60 * (4 if state == "slow" else 1)
                    if n and (m - last_reconcile[o] >= horizon or n > DELTA_MAX_POINTS):
                        res.bytes_down += pull_kb * 1024
                        last_reconcile[o] = m
                        res.snapshot_pulls += 1
                    elif n:
                        res.bytes_down += n * delta_kb * 1024
                        res.delta_pulls += 1
                    last_seen_change[o] = m
                    res.pull_minutes.setdefault(o, []).append(m)

            while budget > 0:
                if cur is None:
                    cur = _next_upload(strategy, pending[o], state, decisions, res, policy, m)
                    if cur is None:
                        break
                budget = _send(res, cur, budget, state)
                if cur.done >= cur.size:
                    c = cur.cap
                    res.photo_at[c.id] = m
                    if strategy != "saakshi":
                        res.record_at[c.id] = m
                        if c.faces and not c.consent:
                            res.faces_unblurred += 1
                        if c.dup_of is not None:
                            res.dup_uploads += 1
                            res.dup_bytes += cur.size
                    elif cur.kind == "compressed":
                        res.compressed += 1
                    cur = None
            current[o] = cur
    return res


def _next_upload(strategy: str, queue: List[Capture], state: str, decisions: Dict[int, Any], res: Result,
                 policy: PolicySettings, now: int = 0) -> Optional[Upload]:
    if not queue:
        return None
    if strategy != "saakshi":
        c = queue.pop(0)                                    # first in, first out, full resolution
        return Upload(c, c.photo_b + int(VECTOR_KB * 1024), "photo")
    queue.sort(key=lambda c: (-decisions[c.id].priority, c.t))
    for i, c in enumerate(queue):
        d = decisions[c.id]
        if c.dup_of is not None and c.dup_of in res.record_at:
            # re-scored at upload time: the original reached the server first
            d2 = decide(facts(c, True), policy)
            if d2.action == "skip_duplicate":
                queue.pop(i)
                res.skipped_dups += 1
                return _next_upload(strategy, queue, state, decisions, res, policy, now)
        constrained = state == "slow"
        if constrained and d.priority < DEFER_BELOW_PRIORITY and (now - c.t) * 60 < DEFER_MAX_WAIT_S:
            continue                                        # routine photo waits for a better link (for a while)
        queue.pop(i)
        if d.action == "sync_blurred":
            return Upload(c, c.blur_b, "blurred")
        if constrained and d.priority < policy.full_res_priority:
            return Upload(c, c.comp_b, "compressed")
        return Upload(c, c.photo_b, "photo")
    return None


# ------------------------------------------------------------------ metrics
def query_metrics(day: Day, strategy: str, res: Result, local_search_ms: float) -> Dict[str, Any]:
    recalls, latencies, failed, answered = [], [], 0, 0
    originals = [c for c in day.captures if c.dup_of is None]
    for q in day.queries:
        relevant = [c for c in originals if c.site == q.site and c.t < q.t]
        state = day.links[q.officer][q.t]
        online = state != "offline"
        latency = None
        if online:
            latency = RTT_S[state] + QUERY_RESPONSE_KB / LINK_KBPS[state]
            online = latency <= QUERY_PATIENCE_S
        if strategy == "cloud":
            if not online:
                failed += 1
                if relevant:
                    recalls.append(0.0)
                continue
            visible = [c for c in relevant if res.record_at.get(c.id, 10 ** 9) <= q.t]
            latencies.append(latency * 1000)
        elif strategy == "queue":
            own = [c for c in relevant if c.officer == q.officer]
            if online:
                visible = [c for c in relevant if c.officer == q.officer or res.record_at.get(c.id, 10 ** 9) <= q.t]
                latencies.append(latency * 1000)
            else:
                visible = own
                latencies.append(local_search_ms)
        else:
            # searched on the device: own captures + the regional mirror as of its last refresh
            last = max((p for p in res.pull_minutes.get(q.officer, []) if p <= q.t), default=-1)
            visible = [c for c in relevant if c.officer == q.officer or res.record_at.get(c.id, 10 ** 9) <= last]
            latencies.append(local_search_ms)
        answered += 1
        if relevant:
            recalls.append(len(visible) / len(relevant))
    return {"queries": len(day.queries), "answered": answered, "failed": failed,
            "team_recall": float(np.mean(recalls)) if recalls else float("nan"),
            "latency_ms_p50": float(np.percentile(latencies, 50)) if latencies else float("nan")}


def summarize(day: Day, strategy: str, res: Result, local_ms: float) -> Dict[str, Any]:
    urgent = [c for c in day.captures if c.urgent]
    delays = [res.photo_at[c.id] - c.t for c in urgent if c.id in res.photo_at]
    alert = [res.record_at[c.id] - c.t for c in urgent if c.id in res.record_at]
    originals = [c for c in day.captures if c.dup_of is None]
    delivered = sum(1 for c in originals if c.id in res.photo_at)
    q = query_metrics(day, strategy, res, local_ms)
    return {
        "captures": len(day.captures),
        "urgent": len(urgent),
        "urgent_delivered": len(delays),
        "urgent_photo_min": delays,
        "urgent_alert_min": alert,
        "originals": len(originals),
        "originals_delivered": delivered,
        "mb_up": res.bytes_up / 1e6,
        "mb_up_slow": res.bytes_up_slow / 1e6,
        "mb_down": res.bytes_down / 1e6,
        "mb_wasted": res.wasted / 1e6,
        "faces_unblurred": res.faces_unblurred,
        "dup_uploads": res.dup_uploads,
        "dup_mb": res.dup_bytes / 1e6,
        "skipped_dups": res.skipped_dups,
        "compressed": res.compressed,
        "delta_pulls": res.delta_pulls,
        "snapshot_pulls": res.snapshot_pulls,
        **q,
    }


def aggregate(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    def mean(k):
        return float(np.nanmean([r[k] for r in rows]))

    urg = [d for r in rows for d in r["urgent_photo_min"]]
    alert = [d for r in rows for d in r["urgent_alert_min"]]
    n_urgent = sum(r["urgent"] for r in rows)
    return {
        "days": len(rows),
        "captures_per_day": mean("captures"),
        "query_answered_pct": 100.0 * sum(r["answered"] for r in rows) / max(1, sum(r["queries"] for r in rows)),
        "team_recall_pct": 100.0 * mean("team_recall"),
        "query_latency_ms_p50": float(np.nanmedian([r["latency_ms_p50"] for r in rows])),
        "urgent_total": n_urgent,
        "urgent_delivered_pct": 100.0 * len(urg) / max(1, n_urgent),
        "urgent_photo_min_p50": float(np.percentile(urg, 50)) if urg else None,
        "urgent_photo_min_p90": float(np.percentile(urg, 90)) if urg else None,
        "urgent_alert_min_p50": float(np.percentile(alert, 50)) if alert else None,
        "originals_delivered_pct": 100.0 * sum(r["originals_delivered"] for r in rows) / max(1, sum(r["originals"] for r in rows)),
        "mb_up_per_day": mean("mb_up"),
        "mb_up_slow_per_day": mean("mb_up_slow"),
        "mb_down_per_day": mean("mb_down"),
        "mb_wasted_per_day": mean("mb_wasted"),
        "faces_unblurred_per_day": mean("faces_unblurred"),
        "dup_uploads_per_day": mean("dup_uploads"),
        "dup_mb_per_day": mean("dup_mb"),
        "skipped_dups_per_day": mean("skipped_dups"),
        "compressed_per_day": mean("compressed"),
        "delta_pulls_per_day": mean("delta_pulls"),
        "snapshot_pulls_per_day": mean("snapshot_pulls"),
    }


def _measured(path_name: str) -> Optional[Dict[str, Any]]:
    p = OUT / f"{path_name}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def run(days: int = 30, seed0: int = 100) -> Dict[str, Any]:
    sync = _measured("sync") or {}
    pull_kb, delta_kb, src = PARTIAL_KB, DELTA_KB_PER_POINT, "defaults in bench_fieldday.py"
    reg = sync.get("regional") or sync.get("global")
    if reg and reg.get("one_point"):
        pull_kb = reg["one_point"]["partial"]["bytes"] / 1024
        delta_kb = (reg.get("incremental_delta") or reg["one_point"]["delta"])["bytes_per_point"] / 1024
        src = f"docs/benchmarks/sync.json ({'regional' if sync.get('regional') else 'global'} layout)"
    search = _measured("search") or {}
    local_ms = 5.0
    if search.get("sizes"):
        row = min(search["sizes"], key=lambda r: abs(r["n"] - 10000))
        local_ms = float(row["edge"]["hybrid_p50_ms"]) + 25.0      # + text embedding on a phone-class CPU (assumed)
    policy = PolicySettings()
    out: Dict[str, Any] = {
        "machine": machine(),
        "assumptions": {k: v for k, v in globals().items() if k.isupper() and isinstance(v, (int, float, tuple, dict, list))},
        "partial_snapshot_kb": round(pull_kb, 1), "delta_kb_per_point": round(delta_kb, 2), "measured_from": src,
        "local_search_ms": round(local_ms, 2),
        "scenarios": {},
    }
    for name, sc in SCENARIOS.items():
        rows: Dict[str, List[Dict[str, Any]]] = {s: [] for s in STRATEGIES}
        for d in range(days):
            day = make_day(seed0 + d, sc)
            for strat in STRATEGIES:
                res = simulate(day, strat, pull_kb, policy, delta_kb)
                rows[strat].append(summarize(day, strat, res, local_ms))
        out["scenarios"][name] = {"label": sc["label"], "mix": sc["mix"], **{s: aggregate(rows[s]) for s in STRATEGIES}}
    return out


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:,.1f}" if not math.isnan(v) else "-"
    return str(v)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()
    out = run(args.days)
    keys = ["query_answered_pct", "team_recall_pct", "query_latency_ms_p50", "urgent_delivered_pct", "urgent_photo_min_p50",
            "urgent_photo_min_p90", "originals_delivered_pct", "mb_up_per_day", "mb_up_slow_per_day", "mb_down_per_day",
            "mb_wasted_per_day", "faces_unblurred_per_day", "dup_uploads_per_day"]
    for sc in out["scenarios"].values():
        print(f"\n{sc['label']}  (link mix {sc['mix']})")
        print(f"  {'metric':28s}" + "".join(f"{s:>12s}" for s in STRATEGIES))
        for k in keys:
            print(f"  {k:28s}" + "".join(f"{_fmt(sc[s][k]):>12s}" for s in STRATEGIES))
    print("\nSaved", save("fieldday", out))


if __name__ == "__main__":
    main()
