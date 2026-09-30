"""Run the whole benchmark suite and rebuild docs/benchmarks/results.json + charts.

    # a standalone Qdrant on 6333 and (optional) a cluster-mode Qdrant on 6433
    python -m bench.run_all --server http://127.0.0.1:6333 --cluster http://127.0.0.1:6433

    python -m bench.run_all --quick     # small sizes, a few minutes on a laptop
"""

from __future__ import annotations

import argparse
import subprocess
import sys


def sh(args) -> None:
    print("\n$", " ".join(args), flush=True)
    subprocess.run([sys.executable, "-m", *args], check=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://127.0.0.1:6333", help="standalone Qdrant (search over HTTP, global-layout sync)")
    ap.add_argument("--cluster", default=None, help="cluster-mode Qdrant (regional shard keys)")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--skip-server", action="store_true", help="device-only numbers (no Qdrant server needed)")
    args = ap.parse_args()

    sizes = ["1000", "10000"] if args.quick else ["1000", "10000", "50000", "100000"]
    search = ["bench.bench_search", "--sizes", *sizes, "--queries", "100" if args.quick else "200"]
    if not args.skip_server:
        search += ["--server", args.server]
    sh(search)
    if not args.skip_server:
        sync = ["bench.bench_sync", "--global-url", args.server, "--per-project", "500" if args.quick else "2000"]
        if args.cluster:
            sync += ["--cluster-url", args.cluster]
        sh(sync)
    sh(["bench.bench_fieldday", "--days", "10" if args.quick else "30"])
    sh(["bench.report"])


if __name__ == "__main__":
    main()
