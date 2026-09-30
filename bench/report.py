"""Combine benchmark results into docs/benchmarks/results.json and draw the charts.

    python -m bench.report            # after bench_search, bench_sync, bench_fieldday
    python -m bench.run_all           # runs everything, then this

The headlines are computed from the measured files, never typed in by hand.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from bench.common import OUT

COLORS = {"cloud": "#8a8f98", "queue": "#d9a441", "saakshi": "#0f8b6f",
          "edge": "#0f8b6f", "edge_int8": "#5cc2a6", "edge_unindexed": "#b9d8cf", "numpy": "#d9a441", "server_http": "#8a8f98"}
NAMES = {"cloud": "Cloud app", "queue": "Offline queue app", "saakshi": "Saakshi",
         "edge": "Qdrant Edge (HNSW)", "edge_int8": "Qdrant Edge int8", "edge_unindexed": "Qdrant Edge, not indexed",
         "numpy": "NumPy brute force", "server_http": "Qdrant server over HTTP (localhost)"}


def _load(name: str) -> Optional[Dict[str, Any]]:
    p = OUT / f"{name}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _no_change(reg: Dict[str, Any]) -> str:
    """What a partial snapshot request costs when nothing changed, as measured (304 on some servers)."""
    nc = reg.get("no_change") or {}
    if nc.get("status") == 304 or not nc.get("bytes"):
        return f"HTTP {nc.get('status', 304)}, 0 bytes"
    return f"HTTP {nc['status']}, {_kb(nc['bytes'])} (this server re-sends the region)"


def _kb(b: float) -> str:
    return f"{b / 1e6:.1f} MB" if b >= 1e6 else f"{b / 1e3:.0f} KB" if b >= 1e3 else f"{b:.0f} B"


def headlines(search, sync, field) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if search and search.get("sizes"):
        big = max(search["sizes"], key=lambda r: r["n"])
        e, srv, npy = big["edge"], big.get("server_http"), big["numpy"]
        line = {"label": f"Search {big['n']:,} photos on the device",
                "value": f"{e['p50_ms']:.1f} ms",
                "detail": f"p50, recall@10 {e['recall_at_10']:.2f}; NumPy brute force {npy['p50_ms']:.1f} ms"}
        if srv:
            line["detail"] += f"; Qdrant server over localhost HTTP {srv['p50_ms']:.1f} ms before any real network"
        out.append(line)
    reg = (sync or {}).get("regional") or (sync or {}).get("global")
    if reg and reg.get("one_point"):
        d1, p1, full = reg["one_point"]["delta"]["bytes"], reg["one_point"]["partial"]["bytes"], reg["first_pull"]["bytes"]
        out.append({"label": "Stay in sync after 1 new photo in the region",
                    "value": _kb(d1),
                    "detail": f"delta pull; partial snapshot {_kb(p1)}; full region snapshot {_kb(full)}; nothing new: {_no_change(reg)}"})
    if reg and sync.get("regional") and sync.get("global"):
        g, r = sync["global"]["first_pull"]["bytes"], sync["regional"]["first_pull"]["bytes"]
        out.append({"label": "First download for a device that serves one region",
                    "value": _kb(r),
                    "detail": f"its region's shard only, instead of {_kb(g)} for the whole collection ({sync['regional']['projects']} regions)"})
    if field:
        rem = field["scenarios"].get("remote")
        if rem:
            out.append({"label": "Remote islands: team searches answered",
                        "value": f"{rem['saakshi']['query_answered_pct']:.0f}%",
                        "detail": f"cloud app {rem['cloud']['query_answered_pct']:.0f}%; the team's evidence found {rem['saakshi']['team_recall_pct']:.0f}% vs {rem['cloud']['team_recall_pct']:.0f}%"})
            out.append({"label": "Remote islands: data pushed through 2G per team-day",
                        "value": f"{rem['saakshi']['mb_up_slow_per_day']:.0f} MB",
                        "detail": f"cloud app {rem['cloud']['mb_up_slow_per_day']:.0f} MB; urgent photo at HQ p90 {rem['saakshi']['urgent_photo_min_p90']:.0f} min vs {rem['cloud']['urgent_photo_min_p90']:.0f} min"})
        city = field["scenarios"].get("city") or rem
        out.append({"label": "Faces uploaded without blur, per team-day",
                    "value": f"{city['saakshi']['faces_unblurred_per_day']:.0f}",
                    "detail": f"cloud app {city['cloud']['faces_unblurred_per_day']:.0f}; repeat shots uploaded {city['saakshi']['dup_uploads_per_day']:.0f} vs {city['cloud']['dup_uploads_per_day']:.0f}"})
    return out


def charts(search, sync, field) -> List[str]:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return []
    plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False, "figure.dpi": 130})
    made: List[str] = []

    if search and search.get("sizes"):
        rows = sorted(search["sizes"], key=lambda r: r["n"])
        ns = [r["n"] for r in rows]
        fig, ax = plt.subplots(figsize=(7.2, 4.2))
        for key in ("edge", "edge_int8", "edge_unindexed", "numpy", "server_http"):
            ys = [r[key]["p50_ms"] if key in r else None for r in rows]
            pts = [(n, y) for n, y in zip(ns, ys) if y is not None]
            if pts:
                ax.plot([p[0] for p in pts], [p[1] for p in pts], marker="o", lw=2.2 if key == "edge" else 1.5,
                        color=COLORS[key], label=NAMES[key])
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xlabel("photos in memory")
        ax.set_ylabel("top-10 search, p50 (ms, log)")
        ax.set_title("Search latency as memory grows (512-d CLIP vectors)")
        ax.legend(frameon=False, fontsize=8)
        fig.tight_layout()
        fig.savefig(OUT / "search_latency.png", metadata={"Software": None})
        plt.close(fig)
        made.append("search_latency.png")

        sweep = search.get("ef_sweep")
        if sweep:
            fig, ax = plt.subplots(figsize=(6.4, 3.8))
            xs = [p["p50_ms"] for p in sweep["points"]]
            ys = [p["recall_at_10"] for p in sweep["points"]]
            ax.plot(xs, ys, marker="o", color=COLORS["edge"], lw=2)
            for p in sweep["points"]:
                ax.annotate(f"ef={p['ef']}", (p["p50_ms"], p["recall_at_10"]), textcoords="offset points", xytext=(6, -12), fontsize=8)
            ax.set_xlabel("p50 latency (ms)")
            ax.set_ylabel("recall@10")
            ax.set_ylim(0.4, 1.02)
            ax.set_title(f"Accuracy vs speed at {sweep['n']:,} photos (Saakshi uses ef=512)")
            fig.tight_layout()
            fig.savefig(OUT / "recall_ef.png", metadata={"Software": None})
            plt.close(fig)
            made.append("recall_ef.png")

    reg = (sync or {}).get("regional") or (sync or {}).get("global")
    if reg and reg.get("one_point"):
        items = [
            ("Full region snapshot\n(first pull)", reg["first_pull"]["bytes"], "#8a8f98"),
            ("Re-download all\n(naive)", reg["naive_repull"]["bytes"], "#8a8f98"),
            ("Partial snapshot\n50 new", reg["incremental_pull"]["bytes"], "#5cc2a6"),
            ("Delta pull\n50 new", reg["incremental_delta"]["bytes"], "#0f8b6f"),
            ("Partial snapshot\n1 new", reg["one_point"]["partial"]["bytes"], "#5cc2a6"),
            ("Delta pull\n1 new", reg["one_point"]["delta"]["bytes"], "#0f8b6f"),
        ]
        fig, ax = plt.subplots(figsize=(7.6, 4.0))
        bars = ax.bar(range(len(items)), [i[1] for i in items], color=[i[2] for i in items])
        ax.set_yscale("log")
        ax.set_xticks(range(len(items)))
        ax.set_xticklabels([i[0] for i in items], fontsize=8)
        ax.set_ylabel("bytes downloaded (log)")
        ax.set_title("What a device downloads to stay in sync with its region")
        ax.set_ylim(top=max(i[1] for i in items) * 6)
        for b, (_, v, _) in zip(bars, items):
            ax.text(b.get_x() + b.get_width() / 2, v * 1.15, _kb(v), ha="center", fontsize=8)
        ax.text(0.99, 0.97, f"nothing changed: {_no_change(reg)}", transform=ax.transAxes, ha="right", va="top", fontsize=8, color="#555")
        fig.tight_layout()
        fig.savefig(OUT / "sync_bytes.png", metadata={"Software": None})
        plt.close(fig)
        made.append("sync_bytes.png")

    if field:
        scen = list(field["scenarios"])
        labels = [field["scenarios"][s]["label"].split(" (")[0] for s in scen]
        strategies = ["cloud", "queue", "saakshi"]

        def grouped(metric: str, title: str, ylabel: str, fname: str, fmt: str = "{:.0f}") -> None:
            fig, ax = plt.subplots(figsize=(7.2, 3.9))
            w = 0.26
            for i, st in enumerate(strategies):
                vals = [field["scenarios"][s][st][metric] or 0 for s in scen]
                bars = ax.bar([x + (i - 1) * w for x in range(len(scen))], vals, w, color=COLORS[st], label=NAMES[st])
                for b, v in zip(bars, vals):
                    ax.text(b.get_x() + b.get_width() / 2, b.get_height(), fmt.format(v), ha="center", va="bottom", fontsize=7)
            ax.set_xticks(range(len(scen)))
            ax.set_xticklabels(labels)
            ax.set_ylabel(ylabel)
            ax.set_title(title)
            ax.legend(frameon=False, fontsize=8, ncol=3, loc="upper center", bbox_to_anchor=(0.5, -0.12))
            fig.tight_layout()
            fig.savefig(OUT / fname, metadata={"Software": None})
            plt.close(fig)
            made.append(fname)

        grouped("team_recall_pct", "Team evidence an officer finds when searching",
                "% of relevant evidence found", "fieldday_recall.png")
        grouped("query_answered_pct", "Searches that get an answer at all", "% of searches", "fieldday_answered.png")
        grouped("mb_up_slow_per_day", "Data pushed through a slow (2G) link, per team-day", "MB", "fieldday_slow_mb.png")
        grouped("urgent_photo_min_p90", "Urgent photo reaches HQ: 90th percentile delay", "minutes", "fieldday_urgent.png")
    return made


def _ms(v: Optional[float]) -> str:
    return "–" if v is None else f"{v:.2f}"


def markdown_tables(search, sync, field) -> str:
    """Result tables for docs/BENCHMARKS.md, generated so the text always matches the JSON."""
    out: List[str] = []
    if search and search.get("sizes"):
        rows = sorted(search["sizes"], key=lambda r: r["n"])
        cols = [("edge", "Qdrant Edge, tuned HNSW"), ("edge_qdrant_defaults", "Qdrant Edge, default HNSW"), ("edge_int8", "Qdrant Edge, int8"),
                ("edge_unindexed", "Qdrant Edge, not indexed"), ("numpy", "NumPy brute force"), ("server_http", "Qdrant server, HTTP, localhost")]
        cols = [c for c in cols if any(c[0] in r for r in rows)]
        out.append("**Top-10 search: p50 latency in ms (recall@10)**\n")
        out.append("| Photos | " + " | ".join(c[1] for c in cols) + " |")
        out.append("|---:|" + "---:|" * len(cols))
        for r in rows:
            cells = [f"{_ms(r[k]['p50_ms'])} ({r[k]['recall_at_10']:.3f})" if k in r else "–" for k, _ in cols]
            out.append(f"| {r['n']:,} | " + " | ".join(cells) + " |")
        out.append("")
        out.append("**Hybrid search (dense + BM25, RRF) on Qdrant Edge, and the device's disk footprint**\n")
        out.append("| Photos | hybrid p50 ms | hybrid p95 ms | index build s | disk MB (float32) | disk MB (+int8) |")
        out.append("|---:|---:|---:|---:|---:|---:|")
        for r in rows:
            e = r["edge"]
            out.append(f"| {r['n']:,} | {_ms(e.get('hybrid_p50_ms'))} | {_ms(e.get('hybrid_p95_ms'))} | {r.get('edge_build_s', '–')} | "
                       f"{r.get('edge_disk_mb', '–')} | {r.get('edge_int8_disk_mb', '–')} |")
        out.append("")
        sw = search.get("ef_sweep")
        if sw:
            out.append(f"**Speed vs accuracy at {sw['n']:,} photos (m={sw.get('m', '?')}, ef_construct={sw.get('ef_construct', '?')})**\n")
            out.append("| hnsw_ef | p50 ms | p95 ms | recall@10 |")
            out.append("|---:|---:|---:|---:|")
            for p in sw["points"]:
                out.append(f"| {p['ef']} | {_ms(p['p50_ms'])} | {_ms(p['p95_ms'])} | {p['recall_at_10']:.3f} |")
            out.append("")
    for layout in ("regional", "global"):
        r = (sync or {}).get(layout)
        if not r:
            continue
        title = (f"**Regional layout** (cluster mode, one shard per project; the device serves 1 of {r['projects']} regions, "
                 f"{r['points_per_project']:,} photos each)" if layout == "regional" else
                 f"**Global layout** (one shard for every project, as on a standalone server; {r['projects']} projects × {r['points_per_project']:,} photos)")
        srv = ((sync or {}).get("server") or {}).get(layout) or {}
        if srv:
            title += f" · server: {srv.get('kind')} {srv.get('version')}, cluster mode {srv.get('cluster_mode')}"
        out.append(title + "\n")
        out.append("| Step | Downloaded | Time |")
        out.append("|---|---:|---:|")
        out.append(f"| First pull: full shard snapshot ({r['first_pull'].get('points', '?'):,} points) | {_kb(r['first_pull']['bytes'])} | {r['first_pull']['ms']:.0f} ms + {r['first_pull'].get('load_ms', 0):.0f} ms to open |")
        out.append(f"| Nothing changed: partial snapshot request | {_kb(r['no_change']['bytes'])} (HTTP {r['no_change']['status']}) | {r['no_change']['ms']:.0f} ms |")
        if r.get("one_point"):
            out.append(f"| 1 new point: delta pull (filtered scroll) | {_kb(r['one_point']['delta']['bytes'])} | {r['one_point']['delta']['ms']:.0f} ms |")
            out.append(f"| 1 new point: partial snapshot | {_kb(r['one_point']['partial']['bytes'])} | {r['one_point']['partial']['ms']:.0f} ms |")
        n_new = sync.get("new_points_per_project", "?")
        if r.get("incremental_delta"):
            out.append(f"| {n_new} new points per region: delta pull | {_kb(r['incremental_delta']['bytes'])} | {r['incremental_delta']['ms']:.0f} ms |")
        out.append(f"| {n_new} new points per region: partial snapshot | {_kb(r['incremental_pull']['bytes'])} | {r['incremental_pull']['ms']:.0f} ms + {r['incremental_pull'].get('apply_ms', 0):.0f} ms to apply |")
        out.append(f"| Naive: download the shard again | {_kb(r['naive_repull']['bytes'])} | {r['naive_repull']['ms']:.0f} ms |")
        out.append("")
    if field:
        keys = [("query_answered_pct", "Searches answered (%)"), ("team_recall_pct", "Team's relevant evidence found (%)"),
                ("query_latency_ms_p50", "Search latency p50 (ms)"), ("urgent_photo_min_p50", "Urgent photo at HQ, p50 (min)"),
                ("urgent_photo_min_p90", "Urgent photo at HQ, p90 (min)"), ("urgent_delivered_pct", "Urgent photos at HQ same day (%)"),
                ("originals_delivered_pct", "All photos at HQ same day (%)"), ("mb_up_per_day", "Uploaded per team-day (MB)"),
                ("mb_up_slow_per_day", "…of which over 2G (MB)"), ("mb_down_per_day", "Downloaded per team-day (MB)"),
                ("mb_wasted_per_day", "Wasted by dropped uploads (MB)"), ("faces_unblurred_per_day", "Unblurred faces uploaded per team-day"),
                ("dup_uploads_per_day", "Repeat shots uploaded per team-day")]
        for sc in field["scenarios"].values():
            mix = ", ".join(f"{k} {int(v * 100)}%" for k, v in sc["mix"].items())
            out.append(f"**{sc['label']}** · link: {mix} · {sc['saakshi']['days']} simulated days, {sc['saakshi']['captures_per_day']:.0f} photos per team-day\n")
            out.append("| Metric | Cloud app | Offline queue app | Saakshi |")
            out.append("|---|---:|---:|---:|")
            for k, label in keys:
                vals = []
                for st in ("cloud", "queue", "saakshi"):
                    v = sc[st].get(k)
                    vals.append("–" if v is None else (f"{v:,.0f}" if abs(v) >= 100 else f"{v:,.1f}"))
                out.append(f"| {label} | " + " | ".join(vals) + " |")
            out.append("")
    return "\n".join(out)


def update_markdown(search, sync, field) -> bool:
    doc = OUT.parent / "BENCHMARKS.md"
    if not doc.exists():
        return False
    text = doc.read_text(encoding="utf-8")
    a, b = "<!-- results:start -->", "<!-- results:end -->"
    if a not in text or b not in text:
        return False
    new = text.split(a)[0] + a + "\n\n" + markdown_tables(search, sync, field) + "\n" + b + text.split(b, 1)[1]
    doc.write_text(new, encoding="utf-8")
    return True


def build() -> Dict[str, Any]:
    search, sync, field = _load("search"), _load("sync"), _load("fieldday")
    made = charts(search, sync, field)
    res = {"generated": time.strftime("%Y-%m-%d %H:%M"), "available": bool(search or sync or field),
           "headlines": headlines(search, sync, field), "charts": made,
           "search": search, "sync": sync, "fieldday": field}
    (OUT / "results.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
    res["markdown_updated"] = update_markdown(search, sync, field)
    return res


def main() -> None:
    res = build()
    for h in res["headlines"]:
        print(f"- {h['label']}: {h['value']}  ({h['detail']})")
    print("charts:", ", ".join(res["charts"]))


if __name__ == "__main__":
    main()
