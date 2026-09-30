"""Edge <-> cloud synchronisation.

Push (device -> cloud), driven by the outbox, highest priority first:
  1. vectors + payload -> Qdrant server (tiny; the cloud learns about the
     evidence even over a 2G link)
  2. photo (original, compressed, or the face-blurred copy) -> Saakshi Impact

  The link is measured on every transfer. On a constrained link (default
  below 128 KB/s) low-priority photos wait and routine photos go compressed;
  urgent items still go at full resolution. Decisions are logged with reasons.

Pull (cloud -> device), following Qdrant's edge sync guide:
  * for each region the device serves (regional layout) or the whole
    collection (global layout): first a full shard snapshot, unpacked as a
    mirror shard; then partial snapshots built from the mirror's manifest
    (HTTP 304 when nothing changed)
  * if the server refuses snapshots, fall back to copying changed points by scroll
  * then process what arrived: colleagues' evidence, status claims, conflict
    resolutions, and the cloud's verdict on our own photos
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

import httpx

from saakshi_core.qdrant_server import GLOBAL, ServerCollection, point_struct, shard_key_for
from saakshi_core.schema import KIND_EVIDENCE, SYNC_SYNCED, VECTOR_BM25, VECTOR_CLIP

from .memory import GLOBAL_KEY
from .merge import merge_payloads
from .service import FieldService, search_text

log = logging.getLogger("saakshi.field.sync")

NETWORK_MODES = ("online", "slow", "offline")
SLOW_LINK_KBPS = 24.0           # simulated 2G uplink
CONSTRAINED_BELOW_KBPS = 128.0  # measured link speed below which the device saves bandwidth
DEFER_BELOW_PRIORITY = 65       # on a constrained link, only urgent photos travel; the rest wait (their vectors do not)
DEFER_MAX_WAIT_S = 45 * 60      # ...but not forever: after this a compressed copy goes even on a slow link

# Pulling a region: a partial snapshot re-sends every segment that changed, and
# even one new point costs a few hundred KB (the appendable segment and its
# indexes). A filtered scroll costs a few KB per point. The device asks the
# server how many points changed and picks the cheaper way (bench/bench_sync.py
# measures both); a snapshot still runs periodically to reconcile deletions and
# anything a skewed device clock hid from the timestamp filter.
DELTA_MAX_POINTS = 100          # up to this many changed points, fetch them one by one (~3 KB each; a partial
                                # snapshot re-sends whole segments, and on Qdrant Cloud the whole region)
DELTA_CAP = 400                 # points held in a region's delta shard before a snapshot folds them in
RECONCILE_S = 15 * 60           # snapshot at least this often on a good link (x4 on a constrained one)
PROBE_TIMEOUT_S = 5.0           # reachability probe; a first HTTPS handshake over 2G can take seconds



def human_bytes(n: float) -> str:
    """3174 -> "3.2 KB", 999700 -> "1.0 MB", 7010816 -> "7.0 MB" (the same rule as the device UI)."""
    n, units, i = max(0.0, float(n or 0)), ["B", "KB", "MB", "GB", "TB"], 0
    while n >= 999.5 and i < len(units) - 1:
        n, i = n / 1000, i + 1
    return f"{round(n) if i == 0 or n >= 99.5 else f'{n:.1f}'} {units[i]}"


def _server_seconds(r: httpx.Response) -> float:
    """Processing time the server reported in its Server-Timing header ("ingest;dur=1234")."""
    return sum(float(ms) for ms in re.findall(r"\bdur=(\d+(?:\.\d+)?)", r.headers.get("server-timing") or "")) / 1000.0


def _raise_with_detail(r: httpx.Response) -> None:
    """Like raise_for_status, but keeps Impact's reason, which the outbox shows to the officer."""
    if r.is_success:
        return
    try:
        detail = r.json().get("detail")
    except ValueError:
        detail = r.text[:200]
    raise RuntimeError(f"HTTP {r.status_code}: {detail or r.reason_phrase}")

class LinkMonitor:
    """Rolling estimate of the uplink, from real (or simulated) transfers."""

    def __init__(self, window: int = 6):
        self.samples: Deque[Tuple[int, float]] = deque(maxlen=window)
        self.rtt_ms: Optional[float] = None

    MIN_SAMPLE_BYTES = 32 * 1024  # smaller transfers measure latency, not bandwidth

    def record(self, nbytes: int, seconds: float) -> None:
        if nbytes >= self.MIN_SAMPLE_BYTES and seconds > 0:
            self.samples.append((nbytes, seconds))

    @property
    def kbps(self) -> Optional[float]:
        if not self.samples:
            return None
        total_b = sum(b for b, _ in self.samples)
        total_s = sum(s for _, s in self.samples)
        return (total_b / 1024.0) / total_s if total_s else None

    def quality(self, online: bool) -> str:
        if not online:
            return "offline"
        k = self.kbps
        if k is None:
            return "unknown"
        return "constrained" if k < CONSTRAINED_BELOW_KBPS else "good"

    def reset(self) -> None:
        self.samples.clear()


class SyncEngine:
    def __init__(self, service: FieldService):
        self.svc = service
        self.s = service.s
        self.db = service.db
        self.link = LinkMonitor()
        self._server: Optional[ServerCollection] = None
        self._online_cache: Optional[tuple] = None
        # One kept-alive client for the "is it reachable?" probes: after the first TLS handshake
        # a probe is a single round trip. A slow link (2G) or a first contact with a cloud server
        # can take seconds, which must not count as "offline".
        self._probe = httpx.Client(timeout=PROBE_TIMEOUT_S)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_pull = 0.0
        self._deferred_logged: set = set()

    # ----------------------------------------------------------- connectivity
    @property
    def network_mode(self) -> str:
        mode = self.db.get("network_mode")
        if mode in NETWORK_MODES:
            return mode
        return "offline" if self.db.get("simulate_offline", False) else "online"

    @property
    def simulated_offline(self) -> bool:
        return self.network_mode == "offline"

    def set_network_mode(self, mode: str) -> None:
        if mode not in NETWORK_MODES:
            raise ValueError(mode)
        self.db.set("network_mode", mode)
        self._online_cache = None
        self.link.reset()
        text = {"online": "Network ON", "slow": f"Network switched to a simulated 2G link ({SLOW_LINK_KBPS:.0f} KB/s)",
                "offline": "Network OFF (simulated)"}[mode]
        self.db.log("network", text, "info")

    def set_simulated_offline(self, value: bool) -> None:
        self.set_network_mode("offline" if value else "online")

    def _headers(self) -> Dict[str, str]:
        return {"api-key": self.s.qdrant_api_key} if self.s.qdrant_api_key else {}

    def reachability(self) -> Dict[str, Optional[bool]]:
        """Which endpoints respond right now (cached for 3 s)."""
        if self.simulated_offline:
            return {"qdrant": False if self.s.qdrant_url else None, "impact": False if self.s.impact_url else None}
        now = time.time()
        if self._online_cache and now - self._online_cache[0] < 3:
            return self._online_cache[1]
        res: Dict[str, Optional[bool]] = {"qdrant": None, "impact": None}
        if self.s.qdrant_url:
            try:
                t0 = time.perf_counter()
                r = self._probe.get(self.s.qdrant_url.rstrip("/") + "/", headers=self._headers())
                self.link.rtt_ms = (time.perf_counter() - t0) * 1000
                res["qdrant"] = r.status_code < 500
            except Exception:
                res["qdrant"] = False
        if self.s.impact_url:
            try:
                r = self._probe.get(self.s.impact_url.rstrip("/") + "/api/health")
                res["impact"] = r.status_code == 200
            except Exception:
                res["impact"] = False
        self._online_cache = (now, res)
        return res

    def is_online(self) -> bool:
        return any(v for v in self.reachability().values())

    @property
    def server(self) -> ServerCollection:
        if self._server is None:
            self._server = ServerCollection(self.s.qdrant_url, self.s.qdrant_api_key, self.s.collection)
        return self._server

    def _constrained(self) -> bool:
        if self.network_mode == "slow":
            return True
        return bool(self.svc.s.policy.auto_bandwidth) and self.link.quality(True) == "constrained"

    def _simulate_transfer(self, nbytes: int, real_seconds: float) -> float:
        """On the simulated 2G link, account for the time the bytes would take (sleeps at most 1.5 s)."""
        if self.network_mode != "slow":
            return real_seconds
        virtual = nbytes / (SLOW_LINK_KBPS * 1024.0)
        time.sleep(min(1.5, max(0.0, virtual - real_seconds)))
        return max(real_seconds, virtual)

    # ------------------------------------------------------------------- push
    def push_once(self, batch: int = 16) -> Dict[str, int]:
        reach = self.reachability()
        q_ok = bool(reach.get("qdrant"))
        i_ok = bool(reach.get("impact"))
        stats = {"vectors": 0, "media": 0, "failed": 0, "deferred": 0}
        if not (q_ok or i_ok):
            return stats
        rows = self.db.due(batch)
        if not rows:
            return stats
        if q_ok:
            try:
                if self.server.ensure():
                    self.db.log("sync", f"Created server collection {self.s.collection} ({self.server.layout} layout)", "info")
            except Exception as exc:
                q_ok = False
                self.db.log("sync", f"Qdrant server not ready: {exc}", "error")

        # 1) vectors, batched per region
        to_upsert, key_of, rows_for_vectors = [], {}, []
        for row in rows:
            if row["vectors_done"]:
                continue
            if not self.s.qdrant_url:
                self.db.mark(row["id"], vectors_done=1)
                row["vectors_done"] = 1
                continue
            if not q_ok:
                continue
            found = self.svc.memory.get(row["evidence_id"], with_vector=True)
            if not found:
                self.db.mark(row["id"], status="done", last_error="evidence deleted")
                continue
            payload, vectors, _ = found
            if row["op"] == "update":
                payload = self._merge_with_server(row["evidence_id"], payload)
            server_payload = {**payload, "sync_state": SYNC_SYNCED, "synced_ts": time.time()}
            for k in ("sync_reasons", "media_evicted"):
                server_payload.pop(k, None)
            clip = (vectors or {}).get(VECTOR_CLIP)
            sparse_v = (vectors or {}).get(VECTOR_BM25)
            sparse = {"indices": list(sparse_v.indices), "values": list(sparse_v.values)} if sparse_v is not None else None
            to_upsert.append(point_struct(row["evidence_id"], list(clip) if clip is not None else None, sparse, server_payload))
            key_of[row["evidence_id"]] = shard_key_for(payload.get("project_id"))
            rows_for_vectors.append(row)
        if to_upsert:
            try:
                t0 = time.perf_counter()
                self.server.upsert(to_upsert, key_of)
                approx = sum(len(json.dumps(p.payload, default=str)) + 512 * 4 for p in to_upsert)
                self.link.record(approx, self._simulate_transfer(approx, time.perf_counter() - t0))
                for row in rows_for_vectors:
                    self.db.mark(row["id"], vectors_done=1)
                    row["vectors_done"] = 1
                stats["vectors"] = len(to_upsert)
                self.db.add_counter("bytes_uploaded", approx)
                regions = sorted(set(key_of.values()))
                where = f" into region shard(s) {', '.join(regions)}" if self.server.layout != GLOBAL else ""
                self.db.log("sync", f"Pushed {len(to_upsert)} point(s) to Qdrant{where}", "info")
            except Exception as exc:
                for row in rows_for_vectors:
                    self.db.fail(row["id"], f"qdrant: {exc}")
                stats["failed"] += len(rows_for_vectors)

        # 2) media, one by one; adapt to the measured link
        constrained = self._constrained()
        for row in rows:
            if not row["vectors_done"]:
                continue
            needs_media = row["op"] == "push" and not row["media_done"] and self.s.impact_url
            if needs_media and not i_ok:
                continue
            waited = time.time() - float(row.get("created_ts") or time.time())
            if needs_media and constrained and row["priority"] < DEFER_BELOW_PRIORITY and waited < DEFER_MAX_WAIT_S:
                stats["deferred"] += 1
                if row["id"] not in self._deferred_logged:
                    self._deferred_logged.add(row["id"])
                    self.db.log("policy", f"Slow link ({self._kbps_text()}): photo {self._label(row)} waits for a better link; "
                                          "its vectors are already on the server", "info", {"evidence_id": row["evidence_id"]})
                continue
            if needs_media:
                try:
                    cloud = self._send_media(row, constrained)
                    self.db.mark(row["id"], media_done=1)
                    stats["media"] += 1
                    if cloud:
                        self.svc.memory.set_payload(row["evidence_id"], {"cloud": cloud})
                except Exception as exc:
                    self.db.fail(row["id"], f"impact: {exc}")
                    stats["failed"] += 1
                    continue
            if row["op"] == "update" and self.s.impact_url:
                if not i_ok:
                    continue  # the edit (it may change consent, i.e. face blurring) waits for Impact
                try:
                    self._send_edit(row["evidence_id"])
                except Exception as exc:
                    self.db.fail(row["id"], f"impact: {exc}")
                    stats["failed"] += 1
                    continue
            self.db.mark(row["id"], status="done", last_error=None)
            self.svc.memory.set_payload(row["evidence_id"], {"sync_state": SYNC_SYNCED, "synced_ts": time.time()})
        if stats["media"]:
            self.svc.enforce_storage_budget()
        return stats

    def _kbps_text(self) -> str:
        k = self.link.kbps
        return f"{k:.0f} KB/s" if k is not None else ("simulated 2G" if self.network_mode == "slow" else "unknown speed")

    def _label(self, row: Dict[str, Any]) -> str:
        found = self.svc.memory.get(row["evidence_id"])
        return (found[0].get("file_name") if found else None) or row["evidence_id"][:8]

    def _merge_with_server(self, evidence_id: str, local: Dict[str, Any]) -> Dict[str, Any]:
        """Before re-uploading an edit, merge with any edit made elsewhere."""
        try:
            recs = self.server.retrieve([evidence_id])
        except Exception:
            return local
        if not recs:
            return local
        remote = dict(recs[0].payload or {})
        merged, taken = merge_payloads(local, remote)
        if taken:
            self.svc.memory.set_payload(evidence_id, {k: merged[k] for k in taken + ["field_ts", "field_by", "version"]})
            self.db.log("merge", f"Merged edit from {', '.join(sorted({(remote.get('field_by') or {}).get(f, '?') for f in taken}))}: {', '.join(taken)}",
                        "info", {"evidence_id": evidence_id})
        return merged

    def _send_media(self, row: Dict[str, Any], constrained: bool) -> Optional[Dict[str, Any]]:
        evidence_id = row["evidence_id"]
        found = self.svc.memory.get(evidence_id)
        if not found:
            return None
        payload, _, _ = found
        if payload.get("kind") != KIND_EVIDENCE:
            return None
        blurred = row["action"] == "sync_blurred"
        path = self.svc.upload_copy_path(evidence_id) if blurred else self.svc.media_path(evidence_id, payload.get("file_ext", ".jpg"))
        if not path.exists():
            raise FileNotFoundError(f"media for {evidence_id} missing on device")
        data = path.read_bytes()
        tier = row["tier"]
        if not blurred and tier == "full" and constrained and row["priority"] < self.s.policy.full_res_priority:
            tier = "compressed"
            self.db.log("policy", f"Slow link ({self._kbps_text()}): sending a compressed copy of {payload.get('file_name')}", "info",
                        {"evidence_id": evidence_id})
        if tier == "compressed" and not blurred:
            from saakshi_core.imaging import compressed_bytes, open_image

            data = compressed_bytes(open_image(data))
        meta = {k: payload.get(k) for k in (
            "evidence_id", "project_id", "site_id", "site_name", "device_id", "device_name", "note", "status_claim",
            "captured_at", "lat", "lon", "faces", "consent", "sha256", "dhash", "camera", "has_exif", "has_gps",
            "edited_with", "file_name", "tags",
        )}
        meta["blurred_on_device"] = blurred
        meta["tier"] = tier
        headers = {"X-Saakshi-Token": self.s.ingest_token} if self.s.ingest_token else {}
        t0 = time.perf_counter()
        r = httpx.post(
            self.s.impact_url.rstrip("/") + "/api/ingest",
            files={"file": (payload.get("file_name") or f"{evidence_id}.jpg", data, "image/jpeg")},
            data={"meta": json.dumps(meta)},
            headers=headers,
            timeout=180.0,
        )
        _raise_with_detail(r)
        # Link speed from time on the network only: Impact reports how long its own processing
        # (Cloudinary upload, analysis) took, which says nothing about this device's link
        on_network = max(0.001, time.perf_counter() - t0 - _server_seconds(r))
        self.link.record(len(data), self._simulate_transfer(len(data), on_network))
        body = r.json()
        self.db.log("sync", f"Uploaded {payload.get('file_name')} to Impact ({'blurred copy' if blurred else tier}, {human_bytes(len(data))})", "info",
                    {"evidence_id": evidence_id, "bytes": len(data), "public_id": body.get("cloud", {}).get("public_id")})
        self.db.add_counter("bytes_uploaded", len(data))
        return body.get("cloud")

    def _send_edit(self, evidence_id: str) -> None:
        found = self.svc.memory.get(evidence_id)
        if not found:
            return
        payload, _, _ = found
        headers = {"X-Saakshi-Token": self.s.ingest_token} if self.s.ingest_token else {}
        r = httpx.patch(self.s.impact_url.rstrip("/") + f"/api/evidence/{evidence_id}",
                        json={k: payload.get(k) for k in ("note", "status_claim", "consent")}, headers=headers, timeout=20.0)
        if r.status_code != 404:  # 404: Impact has not received the photo yet; its upload carries the edit
            _raise_with_detail(r)

    # ------------------------------------------------------------------- pull
    def regions_to_pull(self) -> List[str]:
        """Mirror keys this device should hold, given the server layout and its subscriptions."""
        if self.server.layout == GLOBAL:
            return [GLOBAL_KEY]
        existing = set(self.server.shard_ids())
        return [k for k in self.svc.subscribed_regions() if k in existing]

    def pull_once(self) -> Dict[str, Any]:
        """Refresh every mirror from the server and process what changed."""
        if not self.s.qdrant_url or not self.reachability().get("qdrant"):
            return {"pulled": False, "reason": "server unreachable"}
        try:
            self.server.ensure()
            wanted = self.regions_to_pull()
            ids = self.server.shard_ids()
        except Exception as exc:
            return {"pulled": False, "reason": str(exc)}
        for key in list(self.svc.memory.mirror_keys()):
            if key not in wanted:
                self.svc.memory.drop_mirror(key)
                self.db.log("sync", f"Dropped mirror {key}: this device no longer serves that region", "info")

        results: Dict[str, Any] = {}
        total_bytes, modes = 0, set()
        for key in wanted:
            res = self._pull_region(key, ids.get(key, 0))
            results[key] = res
            total_bytes += int(res.get("bytes") or 0)
            if res.get("mode") and res["mode"] != "none":  # regions with nothing new do not name the pull
                modes.add(res["mode"])
        changed = self._process_mirrors()
        self._last_pull = time.time()
        mode = "+".join(sorted(modes)) or "none"
        self.db.set("last_pull", {"ts": self._last_pull, "mode": mode, "bytes": total_bytes, "changed": changed,
                                  "layout": self.server.layout, "regions": results})
        self.db.add_counter("bytes_downloaded", total_bytes)
        if total_bytes or changed:
            how = {"full": "full snapshot", "partial": "partial snapshot", "delta": "changed points only", "scroll": "scroll"}
            what = " + ".join(how.get(m, m) for m in sorted(modes))
            self.db.log("sync", f"Pulled {len(wanted)} region(s) by {what} ({human_bytes(total_bytes)}), "
                                f"{changed} new or updated item(s)", "info")
        ok = all(r.get("pulled") for r in results.values()) if results else True
        return {"pulled": ok, "mode": mode, "bytes": total_bytes, "changed": changed, "regions": results, "layout": self.server.layout}

    def _pull_region(self, key: str, shard_id: int) -> Dict[str, Any]:
        if self.db.get(f"pull_mode:{key}", self.db.get("pull_mode", "snapshot")) == "scroll":
            return self._pull_scroll(key)
        manifest = self.svc.memory.mirror_manifest(key)
        if manifest is not None:
            plan = self._plan_pull(key)
            if plan["mode"] == "none":
                return {"pulled": True, "mode": "none", "bytes": 0, "copied": 0, "why": plan["why"]}
            if plan["mode"] == "delta":
                res = self._pull_delta(key, plan["since"])
                if res.get("pulled"):
                    res["why"] = plan["why"]
                    return res
        res = self._pull_snapshot(key, shard_id, manifest)
        if res.get("pulled"):
            self.db.set(f"last_snapshot:{key}", time.time())
            self.db.set(f"delta_since:{key}", self.svc.memory.newest_update(key))
        return res

    def _plan_pull(self, key: str) -> Dict[str, Any]:
        """Choose how to bring a region up to date: nothing, a delta, or a snapshot."""
        since = float(self.db.get(f"delta_since:{key}") or 0.0) or self.svc.memory.newest_update(key)
        last = float(self.db.get(f"last_snapshot:{key}") or 0.0)
        horizon = RECONCILE_S * (4 if self._constrained() else 1)
        if time.time() - last >= horizon:
            return {"mode": "snapshot", "why": f"reconcile (last snapshot {int(time.time() - last)} s ago)"}
        n = self._count_since(key, since)
        if n is None:
            return {"mode": "snapshot", "why": "server could not count changes"}
        if n == 0:
            return {"mode": "none", "why": "no changes on the server"}
        if n <= DELTA_MAX_POINTS and self.svc.memory.delta_count(key) + n <= DELTA_CAP:
            return {"mode": "delta", "since": since, "why": f"{n} changed point(s): cheaper one by one than a snapshot"}
        return {"mode": "snapshot", "why": f"{n} changed point(s): a partial snapshot is cheaper"}

    def _region_body(self, key: str, body: Dict[str, Any]) -> Dict[str, Any]:
        if key != GLOBAL_KEY and self.server.layout != GLOBAL:
            body["shard_key"] = key
        return body

    def _count_since(self, key: str, since: float) -> Optional[int]:
        flt = {"must": [{"key": "updated_ts", "range": {"gt": since}}]}
        try:
            r = httpx.post(f"{self.server.url}/collections/{self.server.name}/points/count", headers=self._headers(),
                           json=self._region_body(key, {"filter": flt, "exact": True}), timeout=20.0)
            r.raise_for_status()
            return int(r.json()["result"]["count"])
        except Exception as exc:
            log.debug("count failed for %s: %s", key, exc)
            return None

    def _scroll_changed(self, key: str, since: float):
        """A region's points changed after `since` (all of them when 0), page by page, as
        (Edge points, newest updated_ts, bytes received). Plain REST, so every byte is counted."""
        from qdrant_edge import Point, SparseVector

        offset, newest = None, since
        while True:
            body = self._region_body(key, {"limit": 128, "with_payload": True, "with_vector": True})
            if since:
                body["filter"] = {"must": [{"key": "updated_ts", "range": {"gt": since}}]}
            if offset is not None:
                body["offset"] = offset
            r = httpx.post(f"{self.server.url}/collections/{self.server.name}/points/scroll", headers=self._headers(), json=body, timeout=60.0)
            r.raise_for_status()
            res = r.json()["result"]
            batch = []
            for p in res.get("points", []):
                vec = p.get("vector") or {}
                if not isinstance(vec, dict) or vec.get(VECTOR_CLIP) is None:
                    continue
                v: Dict[str, Any] = {VECTOR_CLIP: list(vec[VECTOR_CLIP])}
                sp = vec.get(VECTOR_BM25)
                if isinstance(sp, dict) and sp.get("indices"):
                    v[VECTOR_BM25] = SparseVector(list(sp["indices"]), list(sp["values"]))
                payload = dict(p.get("payload") or {})
                newest = max(newest, float(payload.get("updated_ts") or 0))
                batch.append(Point(str(p["id"]), v, payload))
            yield batch, newest, r.num_bytes_downloaded
            offset = res.get("next_page_offset")
            if offset is None:
                break

    def _pull_delta(self, key: str, since: float) -> Dict[str, Any]:
        """Fetch the points changed since `since` into the region's delta shard."""
        newest, copied, nbytes = since, 0, 0
        t0 = time.perf_counter()
        try:
            for batch, latest, n in self._scroll_changed(key, since):
                newest, nbytes = latest, nbytes + n
                self.svc.memory.delta_upsert(key, batch)
                copied += len(batch)
        except Exception as exc:
            self.db.log("sync", f"Delta pull failed for {key}: {exc}", "warn")
            return {"pulled": False, "reason": str(exc)}
        self.db.set(f"delta_since:{key}", newest)
        self._simulate_transfer(nbytes, time.perf_counter() - t0)
        return {"pulled": True, "mode": "delta", "bytes": nbytes, "copied": copied}

    def _pull_snapshot(self, key: str, shard_id: int, manifest: Optional[Any]) -> Dict[str, Any]:
        base = self.server.snapshot_url(shard_id)
        tmp = Path(tempfile.mkdtemp(prefix="saakshi-snap-"))
        mode = "partial"
        try:
            t0 = time.perf_counter()
            if manifest is None:
                mode = "full"
                snap = tmp / "shard.snapshot"
                with httpx.stream("GET", base, headers=self._headers(), timeout=180.0) as r:
                    r.raise_for_status()
                    with open(snap, "wb") as f:
                        for chunk in r.iter_bytes():
                            f.write(chunk)
                self.svc.memory.replace_mirror_from_snapshot(key, snap)
            else:
                snap = tmp / "partial.snapshot"
                with httpx.stream("POST", base + "/partial/create", headers=self._headers(), json=manifest, timeout=180.0) as r:
                    if r.status_code == 304:
                        return {"pulled": True, "mode": "partial", "bytes": 0}
                    r.raise_for_status()
                    with open(snap, "wb") as f:
                        for chunk in r.iter_bytes():
                            f.write(chunk)
                self.svc.memory.apply_partial_snapshot(key, snap, tmp)
            size = snap.stat().st_size
            self._simulate_transfer(size, time.perf_counter() - t0)
            return {"pulled": True, "mode": mode, "bytes": size}
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (400, 401, 403, 404, 405, 501) and mode == "full":
                self.db.set(f"pull_mode:{key}", "scroll")
                self.db.log("sync", f"Snapshot download refused for {key} ({exc.response.status_code}); switching to scroll-based mirroring", "warn")
                return self._pull_scroll(key)
            self.db.log("sync", f"Pull failed for {key} ({mode}): {exc}", "error")
            if mode == "partial":
                self.svc.memory.drop_mirror(key)
            return {"pulled": False, "reason": str(exc)}
        except Exception as exc:
            self.db.log("sync", f"Pull failed for {key} ({mode}): {exc}", "error")
            if mode == "partial":
                self.svc.memory.drop_mirror(key)
            return {"pulled": False, "reason": str(exc)}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _pull_scroll(self, key: str) -> Dict[str, Any]:
        """Fallback pull: copy points changed since the last pull into the mirror shard."""
        self.svc.memory.ensure_scroll_mirror(key)
        since = float(self.db.get(f"scroll_watermark:{key}") or 0.0)
        newest, copied, nbytes = since, 0, 0
        try:
            for batch, latest, n in self._scroll_changed(key, since):
                newest, nbytes = latest, nbytes + n
                self.svc.memory.mirror_upsert(key, batch)
                copied += len(batch)
        except Exception as exc:
            self.db.log("sync", f"Pull failed for {key} (scroll): {exc}", "error")
            return {"pulled": False, "reason": str(exc)}
        self.db.set(f"scroll_watermark:{key}", newest)
        if copied:
            self.db.log("sync", f"Mirrored {copied} changed point(s) for {key} by scroll", "info")
        return {"pulled": True, "mode": "scroll", "bytes": nbytes, "copied": copied}

    def _process_mirrors(self) -> int:
        """Look at mirror points newer than the last pull, oldest capture first."""
        from qdrant_edge import FieldCondition, Filter, RangeFloat

        since = float((self.db.get("mirror_watermark") or 0.0))
        newest = since
        changed = 0
        flt = Filter(must=[FieldCondition("updated_ts", range=RangeFloat(gt=since))]) if since else None
        rows = self.svc.memory.scroll_all("mirror", flt)
        rows.sort(key=lambda r: float(r[1].get("captured_ts") or 0))
        for pid, payload, _ in rows:
            newest = max(newest, float(payload.get("updated_ts") or 0))
            if payload.get("device_id") == self.s.device_id:
                self._absorb_cloud_enrichment(pid, payload)
                continue
            first_time = self.db.mark_seen(pid)
            self.svc.apply_remote(payload)
            self.cache_remote_thumb(pid, payload)
            if first_time or payload.get("cloud"):
                changed += 1
        self.db.set("mirror_watermark", newest)
        return changed

    def _absorb_cloud_enrichment(self, pid: str, server_payload: Dict[str, Any]) -> None:
        """Copy AI tags / integrity from the server copy of our own evidence."""
        cloud = server_payload.get("cloud")
        found = self.svc.memory.get(pid)
        if not found or found[2] != "local":
            return
        local = found[0]
        updates: Dict[str, Any] = {}
        new_cloud = bool(cloud) and cloud != local.get("cloud")
        if new_cloud:
            updates["cloud"] = cloud
        merged, taken = merge_payloads(local, server_payload)
        for k in taken:
            updates[k] = merged[k]
        if taken:
            updates.update({"field_ts": merged["field_ts"], "field_by": merged["field_by"], "version": merged["version"]})
            self.db.log("merge", f"Took newer {', '.join(taken)} for {local.get('file_name')} from the server copy", "info")
        if updates:
            self.svc.memory.set_payload(pid, updates)
            if new_cloud:
                lvl = cloud.get("integrity_level")
                self.db.log("hq", f"HQ checked {local.get('file_name')}: {lvl} ({cloud.get('integrity_score')})",
                            "warn" if lvl == "flagged" else "info", {"evidence_id": pid})
                full = self.svc.memory.get(pid, with_vector=True)
                if full and isinstance(full[1], dict) and full[1].get(VECTOR_CLIP) is not None:
                    from saakshi_core.embeddings import bm25_document

                    p = {**full[0]}
                    self.svc.memory.upsert(pid, list(full[1][VECTOR_CLIP]), bm25_document(search_text(p)), p)

    def cache_remote_thumb(self, pid: str, payload: Dict[str, Any]) -> None:
        """Keep a small copy of colleagues' photos so they display offline."""
        url = (payload.get("cloud") or {}).get("thumb_url")
        dest = self.svc.thumb_path(pid)
        if not url or dest.exists():
            return
        if url.startswith("/") and self.s.impact_url:
            url = self.s.impact_url.rstrip("/") + url
        try:
            r = httpx.get(url, timeout=15.0, follow_redirects=True)
            if r.status_code == 200:
                dest.write_bytes(r.content)
        except Exception as exc:  # the photo still shows from the cloud URL when online
            log.debug("Could not cache thumbnail for %s: %s", pid, exc)

    # ------------------------------------------------------------- lifecycle
    def sync_now(self) -> Dict[str, Any]:
        with self._lock:
            self.db.retry_all()
            push = self.push_once(batch=64)
            pull = self.pull_once()
            self._refresh_projects()
            return {"push": push, "pull": pull}

    def _refresh_projects(self) -> None:
        if not self.s.impact_url or not self.reachability().get("impact"):
            return
        try:
            r = httpx.get(self.s.impact_url.rstrip("/") + "/api/projects", timeout=5.0)
            if r.status_code == 200 and r.json().get("projects"):
                self.db.set("projects_cache", {"projects": r.json()["projects"]})
        except Exception as exc:  # keep the cached project list
            log.info("Could not refresh projects from Impact: %s", exc)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="saakshi-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        last_projects = 0.0
        last_maintenance = 0.0
        while not self._stop.is_set():
            try:
                if time.time() - last_maintenance > 30:
                    last_maintenance = time.time()
                    self.svc.maintenance()
                if self.db.get("auto_sync", True) and self.is_online():
                    with self._lock:
                        self.push_once()
                        if time.time() - self._last_pull >= self.s.pull_interval_s:
                            self.pull_once()
                        if time.time() - last_projects > 120:
                            self._refresh_projects()
                            last_projects = time.time()
            except Exception as exc:  # never let the worker die
                log.exception("sync loop error: %s", exc)
            self._stop.wait(self.s.sync_interval_s)

    def server_summary(self) -> Dict[str, Any]:
        """Layout, regions and point count on the server (for the system view, which refreshes
        every few seconds; the three server calls are reused for 10 s)."""
        cached = getattr(self, "_summary_cache", None)
        if cached and time.monotonic() - cached[0] < 10:
            return cached[1]
        if not self.reachability().get("qdrant"):
            return {"reachable": False}
        try:
            self.server.ensure()
            out = {"reachable": True, "layout": self.server.layout, "points": self.server.count(),
                   "regions": sorted(k for k in self.server.shard_ids() if k != GLOBAL_KEY)}
        except Exception as exc:
            return {"reachable": False, "error": str(exc)[:200]}
        self._summary_cache = (time.monotonic(), out)
        return out

    def status(self) -> Dict[str, Any]:
        reach = self.reachability()
        online = any(v for v in reach.values())
        return {
            "online": online,
            "network_mode": self.network_mode,
            "simulated_offline": self.simulated_offline,
            "reachability": reach,
            "qdrant_url": self.s.qdrant_url,
            "impact_url": self.s.impact_url,
            "collection": self.s.collection,
            "layout": self._server._layout if self._server else None,
            "auto_sync": bool(self.db.get("auto_sync", True)),
            "last_pull": self.db.get("last_pull"),
            "link": {"quality": self.link.quality(online), "kbps": self.link.kbps, "rtt_ms": self.link.rtt_ms,
                     "constrained_below_kbps": CONSTRAINED_BELOW_KBPS, "simulated": self.network_mode == "slow"},
            "counters": {"bytes_uploaded": self.db.get("counter:bytes_uploaded", 0),
                         "bytes_downloaded": self.db.get("counter:bytes_downloaded", 0)},
        }
