"""SQLite index for the cloud app.

Cloudinary is the system of record for media and per-asset metadata (tags and
context are written back on every ingest), so this database can be rebuilt
from Cloudinary with `POST /api/admin/rebuild`. It exists for fast filtering,
pairing and reports.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, activity TEXT, description TEXT,
    lat REAL, lon REAL, radius_m REAL, start_date TEXT, end_date TEXT, created_ts REAL
);
CREATE TABLE IF NOT EXISTS sites (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT NOT NULL,
    lat REAL, lon REAL, auto INTEGER DEFAULT 0, created_ts REAL
);
CREATE TABLE IF NOT EXISTS assets (
    id TEXT PRIMARY KEY,
    project_id TEXT, site_id TEXT,
    public_id TEXT, version INTEGER, secure_url TEXT, format TEXT,
    width INTEGER, height INTEGER, bytes INTEGER,
    sha256 TEXT, phash TEXT, dhash TEXT,
    lat REAL, lon REAL, captured_at TEXT, captured_ts REAL, uploaded_at TEXT, uploaded_ts REAL,
    device_id TEXT, device_name TEXT, source TEXT,
    note TEXT, status_claim TEXT, consent INTEGER DEFAULT 0,
    faces INTEGER DEFAULT 0, blurred_on_device INTEGER DEFAULT 0,
    has_exif INTEGER, has_gps INTEGER, camera TEXT, edited_with TEXT,
    ai_tags TEXT, device_tags TEXT, caption TEXT, vision_source TEXT,
    quality REAL, colors TEXT,
    integrity_score INTEGER, integrity_level TEXT, integrity TEXT,
    reuse_of TEXT,
    vector BLOB,
    raw TEXT
);
CREATE INDEX IF NOT EXISTS assets_project ON assets(project_id, site_id, captured_ts);
CREATE TABLE IF NOT EXISTS pairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT, site_id TEXT, before_id TEXT, after_id TEXT,
    gap_days REAL, similarity REAL, added TEXT, removed TEXT, verdict TEXT, summary TEXT,
    composite_url TEXT, composite_transform TEXT, change_text TEXT,
    manual INTEGER DEFAULT 0, created_ts REAL,
    UNIQUE(site_id, manual)
);
CREATE TABLE IF NOT EXISTS derivatives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id TEXT, pair_id INTEGER, purpose TEXT, name TEXT,
    transformation TEXT, url TEXT, created_ts REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL,
    project_id TEXT, asset_id TEXT, level TEXT DEFAULT 'info'
);
"""

JSON_FIELDS = ("ai_tags", "device_tags", "colors", "integrity", "raw", "added", "removed")


class ImpactDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(assets)")}
            for name, decl in (("media_type", "TEXT DEFAULT 'image'"), ("duration", "REAL")):
                if name not in cols:
                    self._conn.execute(f"ALTER TABLE assets ADD COLUMN {name} {decl}")

    def q(self, sql: str, args: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        with self._lock:
            rows = list(self._conn.execute(sql, tuple(args)))
        return [self._decode(dict(r)) for r in rows]

    def x(self, sql: str, args: Iterable[Any] = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, tuple(args))
            return cur.lastrowid or cur.rowcount

    @staticmethod
    def _decode(d: Dict[str, Any]) -> Dict[str, Any]:
        for k in JSON_FIELDS:
            if k in d and isinstance(d[k], str):
                try:
                    d[k] = json.loads(d[k])
                except ValueError:
                    pass
        if "vector" in d:
            blob = d.pop("vector")
            d["_vector"] = np.frombuffer(blob, dtype=np.float32).tolist() if blob else None
        return d

    # --------------------------------------------------------------- projects
    def upsert_project(self, p: Dict[str, Any]) -> None:
        self.x(
            """INSERT INTO projects(id, name, activity, description, lat, lon, radius_m, start_date, end_date, created_ts)
               VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, activity=excluded.activity,
               description=excluded.description, lat=excluded.lat, lon=excluded.lon, radius_m=excluded.radius_m,
               start_date=excluded.start_date, end_date=excluded.end_date""",
            (p["id"], p["name"], p.get("activity"), p.get("description"), p["lat"], p["lon"], p.get("radius_m", 5000),
             p.get("start_date"), p.get("end_date"), time.time()),
        )

    def projects(self) -> List[Dict[str, Any]]:
        return self.q("SELECT * FROM projects ORDER BY created_ts, name")

    def project(self, pid: str) -> Optional[Dict[str, Any]]:
        r = self.q("SELECT * FROM projects WHERE id=?", (pid,))
        return r[0] if r else None

    # ------------------------------------------------------------------ sites
    def upsert_site(self, s: Dict[str, Any]) -> None:
        self.x(
            """INSERT INTO sites(id, project_id, name, lat, lon, auto, created_ts) VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(id) DO NOTHING""",
            (s["id"], s["project_id"], s["name"], s["lat"], s["lon"], 1 if s.get("auto") else 0, time.time()),
        )

    def sites(self, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if project_id:
            return self.q("SELECT * FROM sites WHERE project_id=? ORDER BY name", (project_id,))
        return self.q("SELECT * FROM sites ORDER BY project_id, name")

    def site(self, sid: str) -> Optional[Dict[str, Any]]:
        r = self.q("SELECT * FROM sites WHERE id=?", (sid,))
        return r[0] if r else None

    # ----------------------------------------------------------------- assets
    def insert_asset(self, a: Dict[str, Any], vector: Optional[List[float]]) -> None:
        row = dict(a)
        for k in JSON_FIELDS:
            if k in row and not isinstance(row[k], (str, type(None))):
                row[k] = json.dumps(row[k])
        row["vector"] = np.asarray(vector, dtype=np.float32).tobytes() if vector is not None else None
        cols = ", ".join(row.keys())
        marks = ", ".join("?" for _ in row)
        updates = ", ".join(f"{k}=excluded.{k}" for k in row if k != "id")
        self.x(f"INSERT INTO assets({cols}) VALUES({marks}) ON CONFLICT(id) DO UPDATE SET {updates}", list(row.values()))

    def update_asset(self, aid: str, **fields: Any) -> None:
        for k in JSON_FIELDS:
            if k in fields and not isinstance(fields[k], (str, type(None))):
                fields[k] = json.dumps(fields[k])
        cols = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE assets SET {cols} WHERE id=?", list(fields.values()) + [aid])

    def asset(self, aid: str) -> Optional[Dict[str, Any]]:
        r = self.q("SELECT * FROM assets WHERE id=?", (aid,))
        return r[0] if r else None

    def assets(self, project_id: Optional[str] = None, site_id: Optional[str] = None, level: Optional[str] = None,
               tag: Optional[str] = None, limit: int = 500) -> List[Dict[str, Any]]:
        where, args = [], []
        if project_id:
            where.append("project_id=?")
            args.append(project_id)
        if site_id:
            where.append("site_id=?")
            args.append(site_id)
        if level:
            where.append("integrity_level=?")
            args.append(level)
        if tag:
            where.append("(ai_tags LIKE ? OR device_tags LIKE ?)")
            args += [f'%"{tag}"%', f'%"{tag}"%']
        sql = "SELECT * FROM assets" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY captured_ts DESC LIMIT ?"
        return self.q(sql, args + [limit])

    def all_fingerprints(self) -> List[Dict[str, Any]]:
        return self.q("SELECT id, project_id, site_id, sha256, phash, dhash, captured_at, captured_ts, uploaded_ts, vector FROM assets")

    # ------------------------------------------------------------------ pairs
    def save_pair(self, p: Dict[str, Any]) -> int:
        row = dict(p)
        for k in ("added", "removed"):
            row[k] = json.dumps(row.get(k) or [])
        row.setdefault("manual", 0)
        row["created_ts"] = time.time()
        cols = ", ".join(row.keys())
        marks = ", ".join("?" for _ in row)
        updates = ", ".join(f"{k}=excluded.{k}" for k in row if k not in ("site_id", "manual"))
        self.x(f"INSERT INTO pairs({cols}) VALUES({marks}) ON CONFLICT(site_id, manual) DO UPDATE SET {updates}", list(row.values()))
        r = self.q("SELECT id FROM pairs WHERE site_id=? AND manual=?", (row["site_id"], row["manual"]))
        return int(r[0]["id"]) if r else 0

    def pairs(self, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if project_id:
            return self.q("SELECT * FROM pairs WHERE project_id=? ORDER BY manual DESC, created_ts DESC", (project_id,))
        return self.q("SELECT * FROM pairs ORDER BY created_ts DESC")

    def pair(self, pid: int) -> Optional[Dict[str, Any]]:
        r = self.q("SELECT * FROM pairs WHERE id=?", (pid,))
        return r[0] if r else None

    def delete_auto_pair(self, site_id: str) -> None:
        self.x("DELETE FROM pairs WHERE site_id=? AND manual=0", (site_id,))

    # ------------------------------------------------------------ derivatives
    def add_derivative(self, purpose: str, name: str, transformation: str, url: str,
                       asset_id: Optional[str] = None, pair_id: Optional[int] = None) -> None:
        self.x("INSERT INTO derivatives(asset_id, pair_id, purpose, name, transformation, url, created_ts) VALUES(?,?,?,?,?,?,?)",
               (asset_id, pair_id, purpose, name, transformation, url, time.time()))

    def derivatives_for_pair(self, pair_id: int) -> List[Dict[str, Any]]:
        return self.q("SELECT * FROM derivatives WHERE pair_id=? ORDER BY created_ts DESC", (pair_id,))

    def derivatives_for_asset(self, asset_id: str) -> List[Dict[str, Any]]:
        pair_ids = [r["id"] for r in self.q("SELECT id FROM pairs WHERE before_id=? OR after_id=?", (asset_id, asset_id))]
        rows = self.q("SELECT * FROM derivatives WHERE asset_id=? ORDER BY created_ts DESC", (asset_id,))
        for pid in pair_ids:
            rows += self.q("SELECT * FROM derivatives WHERE pair_id=? ORDER BY created_ts DESC", (pid,))
        return rows

    # ----------------------------------------------------------------- events
    def event(self, kind: str, message: str, project_id: Optional[str] = None, asset_id: Optional[str] = None, level: str = "info") -> None:
        self.x("INSERT INTO events(ts, kind, message, project_id, asset_id, level) VALUES(?,?,?,?,?,?)",
               (time.time(), kind, message, project_id, asset_id, level))

    def events(self, project_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        if project_id:
            return self.q("SELECT * FROM events WHERE project_id=? ORDER BY id DESC LIMIT ?", (project_id, limit))
        return self.q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
