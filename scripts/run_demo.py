"""Start the whole demo with one command: Impact + two field devices.

    python scripts/run_demo.py

    Impact (NGO office)        http://127.0.0.1:8000
    Field device A (Rina)      http://127.0.0.1:8101
    Field device B (Arjun)     http://127.0.0.1:8102

Reads .env for Cloudinary and Qdrant settings. Press Ctrl+C to stop all three.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PROCS = [
    ("Impact", [sys.executable, "-m", "impact", "--port", "8000"]),
    ("Device A", [sys.executable, "-m", "field", "--device", "dev-a", "--name", "Officer Rina", "--port", "8101"]),
    ("Device B", [sys.executable, "-m", "field", "--device", "dev-b", "--name", "Officer Arjun", "--port", "8102"]),
]


def _stop(*_args) -> None:
    raise KeyboardInterrupt


def main() -> None:
    signal.signal(signal.SIGTERM, _stop)  # `kill <pid>` stops all three, like Ctrl+C
    if os.name == "nt":
        signal.signal(signal.SIGBREAK, _stop)  # Ctrl+Break too
    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
    running = []
    # Windows: each app gets its own process group so it can be sent Ctrl+Break,
    # which uvicorn treats like Ctrl+C (a clean shutdown that flushes the shards).
    # terminate() there is TerminateProcess: no shutdown code runs at all.
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    for name, cmd in PROCS:
        print(f"Starting {name}: {' '.join(cmd[1:])}")
        running.append((name, subprocess.Popen(cmd, cwd=str(ROOT), env=env, creationflags=flags)))
        time.sleep(1.5 if name == "Impact" else 0.5)
    print("\n  Impact (NGO office)     http://127.0.0.1:8000"
          "\n  Field device A (Rina)   http://127.0.0.1:8101"
          "\n  Field device B (Arjun)  http://127.0.0.1:8102\n\n  Ctrl+C to stop.\n")
    try:
        while all(p.poll() is None for _, p in running):
            time.sleep(1)
        for name, p in running:
            if p.poll() is not None:
                print(f"{name} exited with code {p.returncode}")
    except KeyboardInterrupt:
        pass
    finally:
        print("\nStopping…")
        for _, p in running:
            if p.poll() is None:
                p.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
        for name, p in running:
            try:
                p.wait(timeout=8)
            except subprocess.TimeoutExpired:
                print(f"{name} did not stop in time; killing it")
                p.kill()


if __name__ == "__main__":
    main()
