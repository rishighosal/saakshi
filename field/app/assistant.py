"""Ask your device: offline answers grounded in the device's Qdrant Edge memory.

The assistant never needs the network. It works in two steps:

1. Retrieve. It recognises what kind of question it is (a site's status,
   conflicts, what is still waiting to sync, HQ verdicts, what is near me) and
   gathers the matching records from the local and mirror shards with the
   same hybrid search the Search tab uses.
2. Answer. By default it writes the answer itself from those records, citing
   each one as [n]. If a small local LLM is running through Ollama
   (OLLAMA_URL and OLLAMA_MODEL), it asks that model to phrase the answer from
   the same numbered records, still offline, and keeps the citations.

Every answer lists its sources so the officer can open the underlying photo.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx

from saakshi_core import taxonomy
from saakshi_core.geo import haversine_m

STATUS_WORDS = ("status", "latest", "happening", "condition", "update", "what about", "how is", "how's", "situation", "report")


def ago(ts: Optional[float]) -> str:
    if not ts:
        return "at an unknown time"
    s = time.time() - float(ts)
    if s < 0:
        return "just now"
    if s < 3600:
        return f"{max(1, int(s // 60))} min ago"
    if s < 86400:
        return f"{int(s // 3600)} h ago"
    days = int(s // 86400)
    return f"{days} day{'s' if days != 1 else ''} ago"


def day(ts: Optional[float]) -> str:
    if not ts:
        return "unknown date"
    return datetime.fromtimestamp(float(ts)).strftime("%d %b")


class Assistant:
    def __init__(self, service):
        self.svc = service

    # --------------------------------------------------------------- helpers
    def _sites(self) -> List[Tuple[str, str]]:
        """(site_id, name) for every site the device knows about."""
        names: Dict[str, str] = {}
        for p in self.svc.projects():
            for s in p.sites:
                names[s.id] = s.name
        for s in self.svc.known_sites():
            names[s.id] = s.name
        names.update(self.svc.db.get("site_names", {}) or {})
        return list(names.items())

    def _match_site(self, q: str) -> Optional[Tuple[str, str]]:
        ql = q.lower()
        best, best_len = None, 0
        for sid, name in self._sites():
            for cand in (name.lower(), sid.lower(), sid.lower().replace("-", " ")):
                if cand and len(cand) > 2 and cand in ql and len(cand) > best_len:
                    best, best_len = (sid, name), len(cand)
        if best:
            return best
        # word overlap, e.g. "embankment" matches "Embankment North"
        words = set(re.findall(r"[a-z]{4,}", ql))
        for sid, name in self._sites():
            nw = set(re.findall(r"[a-z]{4,}", name.lower()))
            if nw and words & nw and not name.lower().startswith("spot "):
                return (sid, name)
        return None

    def _cite(self, cites: List[Dict[str, Any]], item: Dict[str, Any]) -> str:
        for c in cites:
            if c["id"] == item["id"]:
                return f"[{c['n']}]"
        n = len(cites) + 1
        cloud = item.get("cloud") or {}
        cites.append({
            "n": n, "id": item["id"], "label": item.get("note") or item.get("file_name") or "Photo",
            "site": item.get("site_name"), "when": day(item.get("captured_ts")), "who": item.get("device_name") or item.get("device_id"),
            "source": item.get("source"), "hq": cloud.get("integrity_level"),
        })
        return f"[{n}]"

    # ------------------------------------------------------------------- ask
    def ask(self, question: str, lat: Optional[float] = None, lon: Optional[float] = None) -> Dict[str, Any]:
        t0 = time.perf_counter()
        q = (question or "").strip()
        ql = q.lower()
        cites: List[Dict[str, Any]] = []
        facts: List[str] = []
        intent = "search"

        site = self._match_site(q)
        if re.search(r"conflict|disagree|contradict", ql):
            intent = "conflicts"
            facts = self._conflicts(cites)
        elif re.search(r"unsynced|pending|queue|waiting|not (yet )?(synced|uploaded)|to upload|outbox", ql):
            intent = "outbox"
            facts = self._outbox(cites)
        elif re.search(r"flagged|rejected|verified|\bhq\b|headquarters|office said|integrity", ql):
            intent = "hq"
            facts = self._hq(cites)
        elif re.search(r"near me|nearby|around here|close to me", ql) and lat is not None and lon is not None:
            intent = "nearby"
            facts = self._nearby(lat, lon, cites)
        elif site and (any(w in ql for w in STATUS_WORDS) or len(ql.split()) <= 4):
            intent = "site"
            facts = self._site(site, cites)
        else:
            facts = self._search(q, cites)

        answer = "\n".join(facts) if facts else "I could not find anything in this device's memory about that."
        engine = "memory"
        if self.svc.s.ollama_url and self.svc.s.ollama_model and cites:
            llm = self._ollama(q, facts, cites)
            if llm:
                answer, engine = llm, f"local LLM ({self.svc.s.ollama_model})"
        return {
            "question": q, "intent": intent, "answer": answer, "citations": cites, "engine": engine,
            "ms": round((time.perf_counter() - t0) * 1000, 1), "offline": True,
        }

    # -------------------------------------------------------------- intents
    def _site(self, site: Tuple[str, str], cites: List[Dict[str, Any]]) -> List[str]:
        sid, name = site
        tl = self.svc.site_timeline(sid)
        photos = [e for e in tl["events"] if e["type"] == "photo"]
        out: List[str] = []
        cur = tl["current"]
        if cur:
            who = cur["device"]
            if who == self.svc.s.device_id:
                who = f"this device ({self.svc.s.device_name})"
            else:
                who = next((e["who"] for e in tl["events"] if e.get("type") == "photo" and e.get("device_id") == who and e.get("who")), who)
            out.append(f"{name}: current status is {cur['status'].replace('_', ' ')}, reported by {who} {ago(cur['ts'])}.")
        else:
            out.append(f"{name}: no status has been reported yet.")
        if photos:
            items = {i["id"]: i for i in self.svc.list_evidence() if i.get("site_id") == sid}
            recent = sorted(photos, key=lambda e: -(e.get("ts") or 0))[:4]
            verified = sum(1 for e in photos if e.get("hq") == "verified")
            flagged = sum(1 for e in photos if e.get("hq") == "flagged")
            out.append(f"{len(photos)} photo(s) on record; HQ verified {verified}" + (f", flagged {flagged}" if flagged else "") + ".")
            for e in recent:
                it = items.get(e["id"])
                if not it:
                    continue
                status = f" ({e['status'].replace('_', ' ')})" if e.get("status") else ""
                out.append(f"• {day(e['ts'])}, {e['who']}: {e['text']}{status} {self._cite(cites, it)}")
        opened = [e for e in tl["events"] if e["type"] == "conflict" and e.get("state") == "open"]
        if opened:
            out.append(f"Open conflict: {opened[-1]['text']}. Resolve it in the Conflicts tab.")
        return out

    def _conflicts(self, cites: List[Dict[str, Any]]) -> List[str]:
        items = self.svc.db.conflicts("open")
        if not items:
            return ["There are no open conflicts. Every site's reports agree, or newer evidence settled them."]
        out = [f"{len(items)} open conflict(s):"]
        for c in items[:6]:
            out.append(f"• {self.svc.site_name(c['site_id'])}: {c['local_device']} reported {c['local_status'].replace('_', ' ')}, "
                       f"{c['remote_device']} reported {c['remote_status'].replace('_', ' ')} ({ago(c['created_ts'])}).")
            for eid in (c["local_evidence"], c["remote_evidence"]):
                it = self.svc.get(eid)
                if it:
                    self._cite(cites, it)
        return out

    def _outbox(self, cites: List[Dict[str, Any]]) -> List[str]:
        rows = [r for r in self.svc.db.outbox() if r["status"] != "done"]
        if not rows:
            return ["Nothing is waiting: everything that should leave this device has been sent."]
        out = [f"{len(rows)} item(s) waiting to sync, highest priority first:"]
        for r in rows[:6]:
            it = self.svc.get(r["evidence_id"])
            if not it:
                continue
            why = (r.get("reasons") or [""])[0]
            out.append(f"• {it.get('note') or it.get('file_name')}: priority {r['priority']}. {why} {self._cite(cites, it)}")
        held = [i for i in self.svc.list_evidence(include_mirror=False) if i.get("sync_state") in ("held", "local_only")]
        if held:
            out.append(f"{len(held)} more item(s) are private or held on the device by policy and will not be sent.")
        return out

    def _hq(self, cites: List[Dict[str, Any]]) -> List[str]:
        mine = [i for i in self.svc.list_evidence(include_mirror=False) if (i.get("cloud") or {}).get("integrity_level")]
        if not mine:
            return ["HQ has not checked any of this device's photos yet. Verdicts arrive with the next sync."]
        by: Dict[str, List[Dict[str, Any]]] = {}
        for i in mine:
            by.setdefault(i["cloud"]["integrity_level"], []).append(i)
        out = ["HQ verdicts on this device's photos: " + ", ".join(f"{len(v)} {k}" for k, v in sorted(by.items())) + "."]
        for i in by.get("flagged", [])[:4] + by.get("review", [])[:2]:
            out.append(f"• {i['cloud']['integrity_level'].capitalize()}: {i.get('note') or i.get('file_name')} at {i.get('site_name')} {self._cite(cites, i)}")
        return out

    def _nearby(self, lat: float, lon: float, cites: List[Dict[str, Any]]) -> List[str]:
        items = [i for i in self.svc.list_evidence() if i.get("lat") is not None]
        near = sorted(((haversine_m(lat, lon, i["lat"], i["lon"]), i) for i in items), key=lambda t: t[0])[:5]
        near = [(d, i) for d, i in near if d <= 5000]
        if not near:
            return ["No evidence within 5 km of your position in this device's memory."]
        out = ["Evidence near you:"]
        for d, i in near:
            out.append(f"• {d / 1000:.1f} km: {i.get('note') or i.get('file_name')} at {i.get('site_name')} ({day(i.get('captured_ts'))}) {self._cite(cites, i)}")
        return out

    def _search(self, q: str, cites: List[Dict[str, Any]]) -> List[str]:
        res = self.svc.search(q, limit=6)
        hits = res["results"]
        if not hits:
            return []
        statuses: Dict[str, int] = {}
        for h in hits:
            if h.get("status_claim"):
                statuses[h["status_claim"]] = statuses.get(h["status_claim"], 0) + 1
        sites = sorted({h.get("site_name") for h in hits if h.get("site_name")})
        out = [f"Found {len(hits)} relevant record(s) across {len(sites)} site(s) in {res['latency_ms']} ms, without the network."]
        if statuses:
            out.append("Reported statuses among them: " + ", ".join(f"{k.replace('_', ' ')} ×{v}" for k, v in statuses.items()) + ".")
        for h in hits[:5]:
            tags = (h.get("cloud") or {}).get("ai_tags") or h.get("tags") or []
            tag_txt = f" [{', '.join(taxonomy.label(t).lower() for t in tags[:2])}]" if tags else ""
            who = "you" if h.get("source") == "local" else (h.get("device_name") or h.get("device_id"))
            out.append(f"• {h.get('site_name')}, {day(h.get('captured_ts'))}, by {who}: {h.get('note') or h.get('file_name')}{tag_txt} {self._cite(cites, h)}")
        return out

    # ---------------------------------------------------------------- LLM
    def _ollama(self, q: str, facts: List[str], cites: List[Dict[str, Any]]) -> Optional[str]:
        records = "\n".join(f"[{c['n']}] {c['when']}, {c['site']}, by {c['who']}: {c['label']} (HQ: {c['hq'] or 'not checked'})" for c in cites)
        prompt = (
            "You are the assistant on a field officer's device for an NGO. Answer the question using ONLY the facts and "
            "numbered records below. Cite records like [1]. If the records do not answer it, say so. Be brief and plain.\n\n"
            f"Facts:\n{chr(10).join(facts)}\n\nRecords:\n{records}\n\nQuestion: {q}\nAnswer:"
        )
        try:
            r = httpx.post(self.svc.s.ollama_url.rstrip("/") + "/api/generate",
                           json={"model": self.svc.s.ollama_model, "prompt": prompt, "stream": False, "options": {"temperature": 0.2}},
                           timeout=90.0)
            r.raise_for_status()
            text = (r.json().get("response") or "").strip()
            return text or None
        except Exception:
            return None
