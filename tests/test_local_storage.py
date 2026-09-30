"""The device shard on disk: small on Windows (NTFS does not keep Qdrant's preallocated files sparse) and durable when the process dies."""

import random

import pytest

from field.app.diskspace import IS_WINDOWS, file_allocated_bytes
from field.app.memory import DeviceMemory, _dir_bytes
from saakshi_core.schema import CLIP_DIM


def _fill(mem: DeviceMemory, n: int, flush: bool = True) -> None:
    rnd = random.Random(7)
    for i in range(n):
        vec = [rnd.uniform(-1, 1) for _ in range(CLIP_DIM)]
        mem.upsert(f"00000000-0000-0000-0000-{i:012d}", vec, ([1, 2 + i], [1.0, 0.5]), {"note": f"photo {i}"})
    if flush:
        mem.flush()


def test_allocated_bytes_never_exceed_length(tmp_path):
    mem = DeviceMemory(tmp_path)
    _fill(mem, 5)
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert file_allocated_bytes(f) <= max(f.stat().st_size, 1 << 16)
    mem.close()


@pytest.mark.skipif(not IS_WINDOWS, reason="Linux and macOS keep the files sparse already")
def test_reopened_shard_releases_preallocated_space_and_keeps_data(tmp_path):
    mem = DeviceMemory(tmp_path)
    _fill(mem, 20)
    before = _dir_bytes(tmp_path / "local")
    mem.close()

    mem = DeviceMemory(tmp_path)
    after = _dir_bytes(tmp_path / "local")
    assert mem.count("local") == 20
    assert after < before / 4, (before, after)

    # still writable after the files became sparse
    _fill(mem, 30)
    assert mem.count("local") == 30
    mem.close()


_WRITER = """
import os, sys
sys.path.insert(0, sys.argv[2])
from tests.test_local_storage import _fill
from field.app.memory import DeviceMemory
mem = DeviceMemory(sys.argv[1])
_fill(mem, 5, flush=False)
mem.set_payload("00000000-0000-0000-0000-000000000000", {"note": "edited"})
import time; time.sleep(mem.FLUSH_DELAY_S + 1)  # the batched flush of payload updates
os._exit(0)  # no close(): like a crash, a power cut or a killed process
"""


def test_captures_survive_a_process_that_dies_without_closing(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    subprocess.run([sys.executable, "-c", _WRITER, str(tmp_path), str(root)], check=True, cwd=root, timeout=120)
    mem = DeviceMemory(tmp_path)
    assert mem.count("local") == 5
    payload, _, _ = mem.get("00000000-0000-0000-0000-000000000000")
    assert payload["note"] == "edited"
    mem.close()


def test_a_data_folder_too_deep_for_windows_is_reported(monkeypatch):
    from pathlib import PureWindowsPath

    from field.app import diskspace

    monkeypatch.setattr(diskspace, "IS_WINDOWS", True)
    monkeypatch.setattr(diskspace, "_long_paths_enabled", lambda: False)
    short = PureWindowsPath(r"C:\saakshi-data\field\dev-a\memory\mirror")
    deep = PureWindowsPath(r"D:\field-teams\green-delta\laptops\officer-laptop-07\applications\saakshi-field\data\field\dev-a\memory\mirror")
    assert diskspace.shard_path_problem(short) is None
    msg = diskspace.shard_path_problem(deep)
    assert msg and "SAAKSHI_DATA_DIR" in msg
    monkeypatch.setattr(diskspace, "_long_paths_enabled", lambda: True)
    assert diskspace.shard_path_problem(deep) is None
