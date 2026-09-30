"""Device-local bookkeeping in SQLite: outbox, activity log, site status, conflicts.

Vectors and payloads live in Qdrant Edge; this database only tracks work
queues and history, which must survive restarts and power cuts.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .conflicts import Claim

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_id TEXT NOT NULL,
    op TEXT NOT NULL DEFAULT 'push',
    action TEXT NOT NULL DEFAULT 'sync',
    tier TEXT NOT NULL DEFAULT 'full',
    priority INTEGER NOT NULL DEFAULT 50,
    status TEXT NOT NULL DEFAULT 'queued',
    vectors_done INTEGER NOT NULL DEFAULT 0,
    media_done INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    next_attempt_ts REAL NOT NULL DEFAULT 0,
    reasons TEXT,
    created_ts REAL NOT NULL,
    updated_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS outbox_status ON outbox(status, priority DESC, created_ts);

CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    data TEXT
);

CREATE TABLE IF NOT EXISTS site_state (
    site_id TEXT PRIMARY KEY,
    project_id TEXT,
    status TEXT NOT NULL,
    ts REAL NOT NULL,
    device_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    source TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id TEXT NOT NULL,
    project_id TEXT,
    local_status TEXT, local_ts REAL, local_device TEXT, local_evidence TEXT,
    remote_status TEXT, remote_ts REAL, remote_device TEXT, remote_evidence TEXT,
    message TEXT,
    state TEXT NOT NULL DEFAULT 'open',
    resolution TEXT,
    resolved_ts REAL,
    created_ts REAL NOT NULL,
    UNIQUE(site_id, local_evidence, remote_evidence)
);

CREATE TABLE IF NOT EXISTS seen_remote (
    evidence_id TEXT PRIMARY KEY,
    ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class DeviceDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def _q(self, sql: str, args: tuple = ()) -> List[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, args))

    def _x(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, args)
            return cur.lastrowid or cur.rowcount

    # ------------------------------------------------------------------- kv
    def get(self, key: str, default: Any = None) -> Any:
        rows = self._q("SELECT value FROM kv WHERE key=?", (key,))
        if not rows:
            return default
        try:
            return json.loads(rows[0]["value"])
        except (TypeError, ValueError):
            return rows[0]["value"]

    def set(self, key: str, value: Any) -> None:
        self._x("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    def add_counter(self, name: str, amount: float) -> None:
        with self._lock:
            self.set(f"counter:{name}", float(self.get(f"counter:{name}", 0) or 0) + float(amount))

    # --------------------------------------------------------------- outbox
    def enqueue(self, evidence_id: str, action: str, tier: str, priority: int, reasons: List[str], op: str = "push") -> int:
        now = time.time()
        with self._lock:  # check and write as one step: the sync loop and requests both enqueue
            existing = self._q("SELECT id FROM outbox WHERE evidence_id=? AND status IN ('queued','failed') AND op=?", (evidence_id, op))
            if existing:
                self._x(
                    "UPDATE outbox SET action=?, tier=?, priority=?, reasons=?, status='queued', vectors_done=0, updated_ts=? WHERE id=?",
                    (action, tier, priority, json.dumps(reasons), now, existing[0]["id"]),
                )
                return int(existing[0]["id"])
            return self._x(
                "INSERT INTO outbox(evidence_id, op, action, tier, priority, status, reasons, created_ts, updated_ts) VALUES(?,?,?,?,?,'queued',?,?,?)",
                (evidence_id, op, action, tier, priority, json.dumps(reasons), now, now),
            )

    def due(self, limit: int = 16) -> List[Dict[str, Any]]:
        rows = self._q(
            "SELECT * FROM outbox WHERE status IN ('queued','failed') AND next_attempt_ts <= ? ORDER BY priority DESC, created_ts ASC LIMIT ?",
            (time.time(), limit),
        )
        return [dict(r) for r in rows]

    def mark(self, row_id: int, **fields: Any) -> None:
        fields["updated_ts"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        self._x(f"UPDATE outbox SET {cols} WHERE id=?", tuple(fields.values()) + (row_id,))

    def fail(self, row_id: int, error: str) -> None:
        with self._lock:
            row = self._q("SELECT attempts FROM outbox WHERE id=?", (row_id,))
            attempts = (row[0]["attempts"] if row else 0) + 1
            backoff = min(300.0, 5.0 * (2 ** (attempts - 1)))
            self.mark(row_id, status="failed", attempts=attempts, last_error=error[:500], next_attempt_ts=time.time() + backoff)

    def outbox(self, include_done: bool = False, limit: int = 200) -> List[Dict[str, Any]]:
        where = "" if include_done else "WHERE status != 'done'"
        rows = self._q(f"SELECT * FROM outbox {where} ORDER BY CASE status WHEN 'done' THEN 1 ELSE 0 END, priority DESC, created_ts ASC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = dict(r)
            d["reasons"] = json.loads(d["reasons"] or "[]")
            out.append(d)
        return out

    def outbox_counts(self) -> Dict[str, int]:
        rows = self._q("SELECT status, COUNT(*) AS n FROM outbox GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    def retry_all(self) -> None:
        self._x("UPDATE outbox SET next_attempt_ts=0 WHERE status IN ('queued','failed')")

    # -------------------------------------------------------------- activity
    def log(self, kind: str, message: str, level: str = "info", data: Optional[Dict[str, Any]] = None) -> None:
        self._x("INSERT INTO activity(ts, level, kind, message, data) VALUES(?,?,?,?,?)", (time.time(), level, kind, message, json.dumps(data or {})))

    def activity(self, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self._q("SELECT * FROM activity ORDER BY id DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = dict(r)
            d["data"] = json.loads(d["data"] or "{}")
            out.append(d)
        return out

    # ------------------------------------------------------------ site state
    def site_claim(self, site_id: str) -> Optional[Claim]:
        rows = self._q("SELECT * FROM site_state WHERE site_id=?", (site_id,))
        if not rows:
            return None
        r = rows[0]
        return Claim(site_id=r["site_id"], status=r["status"], ts=r["ts"], device_id=r["device_id"], evidence_id=r["evidence_id"])

    def set_site_claim(self, claim: Claim, project_id: Optional[str], source: str) -> None:
        self._x(
            """INSERT INTO site_state(site_id, project_id, status, ts, device_id, evidence_id, source) VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(site_id) DO UPDATE SET status=excluded.status, ts=excluded.ts, device_id=excluded.device_id,
               evidence_id=excluded.evidence_id, source=excluded.source, project_id=excluded.project_id""",
            (claim.site_id, project_id, claim.status, claim.ts, claim.device_id, claim.evidence_id, source),
        )

    def site_states(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in self._q("SELECT * FROM site_state ORDER BY ts DESC")]

    # ------------------------------------------------------------- conflicts
    def add_conflict(self, site_id: str, project_id: Optional[str], local: Claim, remote: Claim, message: str) -> Optional[int]:
        try:
            return self._x(
                """INSERT INTO conflicts(site_id, project_id, local_status, local_ts, local_device, local_evidence,
                   remote_status, remote_ts, remote_device, remote_evidence, message, created_ts)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (site_id, project_id, local.status, local.ts, local.device_id, local.evidence_id,
                 remote.status, remote.ts, remote.device_id, remote.evidence_id, message, time.time()),
            )
        except sqlite3.IntegrityError:
            return None

    def conflicts(self, state: Optional[str] = None) -> List[Dict[str, Any]]:
        if state:
            rows = self._q("SELECT * FROM conflicts WHERE state=? ORDER BY created_ts DESC", (state,))
        else:
            rows = self._q("SELECT * FROM conflicts ORDER BY CASE state WHEN 'open' THEN 0 ELSE 1 END, created_ts DESC")
        return [dict(r) for r in rows]

    def conflict(self, cid: int) -> Optional[Dict[str, Any]]:
        rows = self._q("SELECT * FROM conflicts WHERE id=?", (cid,))
        return dict(rows[0]) if rows else None

    def resolve_conflict(self, cid: int, resolution: str) -> None:
        self._x("UPDATE conflicts SET state='resolved', resolution=?, resolved_ts=? WHERE id=?", (resolution, time.time(), cid))

    def supersede_conflicts(self, site_id: str, newer_than_ts: float) -> int:
        """Close open conflicts at a site when strictly newer evidence arrives."""
        return self._x(
            "UPDATE conflicts SET state='resolved', resolution='superseded', resolved_ts=? "
            "WHERE site_id=? AND state='open' AND local_ts < ? AND remote_ts < ?",
            (time.time(), site_id, newer_than_ts, newer_than_ts),
        )

    # ----------------------------------------------------------- remote seen
    def mark_seen(self, evidence_id: str) -> bool:
        """Returns True the first time an id is seen."""
        try:
            self._x("INSERT INTO seen_remote(evidence_id, ts) VALUES(?, ?)", (evidence_id, time.time()))
            return True
        except sqlite3.IntegrityError:
            return False
