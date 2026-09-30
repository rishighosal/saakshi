"""Disk usage of Qdrant Edge shards on Windows.

Qdrant preallocates its storage files (32 MB vector chunks, payload pages and WAL
segments). Linux and macOS keep the unused part sparse; NTFS allocates all of it,
so a mirror of a region with a handful of photos takes ~700 MB. Before a shard is
opened we mark its files sparse and release their all-zero 64 KB ranges: reads
return the same bytes and later writes allocate on demand. Files must not be
memory-mapped at that moment, so this only runs on closed shards.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger("saakshi.field.diskspace")

IS_WINDOWS = os.name == "nt"
_BLOCK = 1 << 16          # NTFS sparse allocation unit
_MIN_FILE = 1 << 20       # smaller files are not worth scanning

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                 wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _k32.GetCompressedFileSizeW.restype = wintypes.DWORD
    _k32.GetCompressedFileSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    _k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                     ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]

    _FSCTL_SET_SPARSE = 0x000900C4
    _FSCTL_SET_ZERO_DATA = 0x000980C8
    _INVALID_HANDLE = wintypes.HANDLE(-1).value

    class _ZeroData(ctypes.Structure):
        _fields_ = [("start", ctypes.c_longlong), ("end", ctypes.c_longlong)]


# Qdrant Edge nests files up to ~122 characters below a shard folder (measured), and Windows
# without long-path support refuses paths of 260 characters or more.
MAX_SHARD_DIR_CHARS = 120
LONGEST_REGION_KEY = 48  # project ids are slugs of at most 48 characters


def _long_paths_enabled() -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as k:
            return winreg.QueryValueEx(k, "LongPathsEnabled")[0] == 1
    except OSError:
        return False


def shard_path_problem(shard_parent: Path) -> Optional[str]:
    """A plain-language warning when shard folders under `shard_parent` could be too deep for Windows."""
    if not IS_WINDOWS or _long_paths_enabled():
        return None
    n = len(str(shard_parent)) + 1 + LONGEST_REGION_KEY
    if n <= MAX_SHARD_DIR_CHARS:
        return None
    return (f"The data folder is too deep for Windows: region folders can reach {n} characters and must stay under "
            f"{MAX_SHARD_DIR_CHARS}. Set SAAKSHI_DATA_DIR to a short folder such as C:\\saakshi-data, or enable Windows long paths.")


def file_allocated_bytes(f: Path) -> int:
    """Bytes the file occupies on disk (not its length)."""
    st = f.stat()
    if IS_WINDOWS:
        hi = wintypes.DWORD(0)
        ctypes.set_last_error(0)
        lo =_k32.GetCompressedFileSizeW(str(f), ctypes.byref(hi))
        if lo != 0xFFFFFFFF or ctypes.get_last_error() == 0:
            return (hi.value << 32) + lo
        return st.st_size
    return min(st.st_size, getattr(st, "st_blocks", st.st_size // 512 + 1) * 512)


def _zero_ranges(f: Path, size: int):
    zero = bytes(_BLOCK)
    start = None
    with open(f, "rb") as fh:
        off = 0
        while off < size:
            b = fh.read(_BLOCK)
            if not b:
                break
            if b == zero[: len(b)]:
                if start is None:
                    start = off
            elif start is not None:
                yield start, off
                start = None
            off += len(b)
    if start is not None:
        yield start, size


def _release_file(f: Path) -> None:
    size = f.stat().st_size
    if size < _MIN_FILE or file_allocated_bytes(f) < _BLOCK * 2:
        return
    ranges = list(_zero_ranges(f, size))
    if not ranges:
        return
    # GENERIC_READ | GENERIC_WRITE, share read/write/delete, OPEN_EXISTING
    h = _k32.CreateFileW(str(f), 0xC0000000, 0x7, None, 3, 0x80, None)
    if h == _INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        done = wintypes.DWORD()
        if not _k32.DeviceIoControl(h, _FSCTL_SET_SPARSE, None, 0, None, 0, ctypes.byref(done), None):
            raise ctypes.WinError(ctypes.get_last_error())
        for s, e in ranges:
            z = _ZeroData(s, e)
            if not _k32.DeviceIoControl(h, _FSCTL_SET_ZERO_DATA, ctypes.byref(z), ctypes.sizeof(z), None, 0,
                                        ctypes.byref(done), None):
                raise ctypes.WinError(ctypes.get_last_error())
    finally:
        _k32.CloseHandle(h)


def release_unused_space(root: Path) -> None:
    """Make a closed shard's preallocated files sparse (Windows only; no-op elsewhere)."""
    if not IS_WINDOWS or not root.exists():
        return
    for f in root.rglob("*"):
        if f.is_file():
            try:
                _release_file(f)
            except OSError as exc:  # best effort: the shard works either way
                log.debug("Could not release unused space in %s: %s", f, exc)
