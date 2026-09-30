/* Saakshi Field: device UI logic. Plain JavaScript, no external libraries (works offline). */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const state = { tab: "memory", memFilter: "all", status: null, projects: [], sites: [], pickedFiles: [], lastQuery: null, bench: null, excluded: {} };

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const label = (s) => String(s || "").replace(/_/g, " ");
  const cap = (s) => { s = label(s); return s.charAt(0).toUpperCase() + s.slice(1); };
  // 3174 -> "3.2 KB", 999700 -> "1.0 MB" (never "1000 KB"), 7010816 -> "7.0 MB"
  const bytes = (b) => {
    b = Math.max(0, Number(b) || 0);
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (b >= 999.5 && i < units.length - 1) { b /= 1000; i += 1; }
    return `${i === 0 || b >= 99.5 ? Math.round(b) : b.toFixed(1)} ${units[i]}`;
  };
  const store = {
    get(k) { try { return window.localStorage.getItem(k); } catch (_) { return null; } },
    set(k, v) { try { window.localStorage.setItem(k, v); } catch (_) { /* storage unavailable */ } },
  };

  async function api(path, opts = {}) {
    const res = await fetch(path, opts);
    if (!res.ok) {
      let msg = res.statusText;
      try { msg = (await res.json()).detail || msg; } catch (_) { /* not json */ }
      throw new Error(msg);
    }
    return res.json();
  }
  const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  const patch = (path, body) => api(path, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });

  function toast(msg) {
    const t = $("#toast");
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { t.hidden = true; }, 3600);
  }

  function ago(ts) {
    if (!ts) return "";
    const s = Date.now() / 1000 - ts;
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    return `${Math.floor(s / 86400)} d ago`;
  }
  function when(iso) {
    if (!iso) return "";
    const d = typeof iso === "number" ? new Date(iso * 1000) : new Date(iso);
    if (isNaN(d)) return iso;
    return d.toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  }

  const STATE_TEXT = {
    synced: "Synced", queued: "Waiting to sync", held: "Held on device", local_only: "Private",
    skipped_duplicate: "Repeat shot, linked",
  };
  const PULL_TEXT = { full: "full snapshot", partial: "partial snapshot", delta: "changed points only", scroll: "scroll", none: "nothing new" };

  // ------------------------------------------------------------------ status
  async function refreshStatus() {
    try {
      const s = await api("/api/status");
      state.status = s;
      $("#device-name").textContent = `${s.device_name} · ${s.device_id}`;
      document.title = `Saakshi Field · ${s.device_name}`;
      const sync = s.sync;
      const mode = sync.network_mode || (sync.simulated_offline ? "offline" : "online");
      $$("#net-mode button").forEach((b) => { b.classList.toggle("on", b.dataset.mode === mode); b.setAttribute("aria-checked", b.dataset.mode === mode); });
      const pill = $("#net-pill");
      pill.classList.toggle("online", sync.online && mode === "online");
      pill.classList.toggle("slow", sync.online && mode === "slow");
      pill.classList.toggle("offline", !sync.online);
      const link = sync.link || {};
      const speed = link.kbps ? ` · ${Math.round(link.kbps)} KB/s` : "";
      let text;
      if (mode === "offline") text = "Offline · everything still works";
      else if (!sync.online) text = "No server reachable";
      else if (mode === "slow") text = `2G link (simulated)${speed}`;
      else text = `Online${link.quality === "constrained" ? " · slow link" : ""}${speed}`;
      $("#net-label").textContent = text;

      const out = s.outbox || {};
      const waiting = (out.queued || 0) + (out.failed || 0);
      $("#b-outbox").textContent = waiting || "";
      $("#b-conflicts").textContent = s.open_conflicts || "";
      const st = s.sync_states || {};
      const m = s.memory;
      const deltas = Object.values(m.delta_points || {}).reduce((a, b) => a + b, 0);
      $("#stats").innerHTML = [
        ["Photos taken on this device", m.local_points],
        ["Colleagues' photos (mirrors)", m.mirror_ready ? (m.colleague_points ?? m.mirror_points + deltas) : "not pulled yet"],
        ["Regions mirrored", Object.keys(m.mirrors || {}).length],
        ["Search index", m.local_indexed ? `HNSW, ${m.local_indexed} vectors` : "exact (small memory)"],
        ["Synced", st.synced || 0],
        ["Waiting to sync", st.queued || 0],
        ["Held for privacy", st.held || 0],
        ["Repeat shots linked", st.skipped_duplicate || 0],
      ].map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
      const sto = s.storage || {};
      const used = sto.originals_bytes || 0;
      const budget = sto.budget_bytes || 1;
      const pct = Math.min(100, Math.round((used / budget) * 100));
      $("#storage").innerHTML = `<div class="bar" title="Originals on the device vs budget"><i style="width:${pct}%"></i></div>
        <div class="small muted">Originals ${bytes(used)} of ${bytes(budget)} · memory ${bytes((sto.local_shard_bytes || 0) + (sto.mirror_bytes || 0))}${sto.originals_released ? ` · ${sto.originals_released} released (safe in the cloud)` : ""}</div>
        ${m.path_problem ? `<div class="why" role="alert">${esc(m.path_problem)}</div>` : ""}`;
      $("#model").textContent = `Embeddings: ${s.embedder.label}. Search runs on this device with Qdrant Edge${s.assistant && s.assistant.llm ? `; answers phrased by ${s.assistant.llm} (local)` : ""}.`;

      const p = s.policy;
      if (!$("#dup").matches(":active")) {
        $("#dup").value = p.duplicate_threshold;
        $("#dup-val").textContent = `${Math.round(p.duplicate_threshold * 100)}%`;
      }
      if (!$("#budget").matches(":active")) {
        $("#budget").value = p.media_budget_mb;
        $("#budget-val").textContent = bytes(p.media_budget_mb * 1024 * 1024);
      }
      $("#privacy").value = p.privacy_mode;
      $("#metered").checked = !!p.metered;
      $("#autobw").checked = p.auto_bandwidth !== false;
      $("#autosync").checked = !!sync.auto_sync;
      if ($("#status").options.length <= 1) {
        for (const c of s.status_claims) {
          $("#status").insertAdjacentHTML("beforeend", `<option value="${c}">${cap(c)}</option>`);
          $("#o-status").insertAdjacentHTML("beforeend", `<option value="${c}">${cap(c)}</option>`);
        }
      }
    } catch (e) {
      $("#net-label").textContent = "Device app not responding";
    }
  }

  async function loadProjects() {
    const { projects } = await api("/api/projects");
    state.projects = projects;
    const sel = $("#project");
    const keep = sel.value;
    sel.innerHTML = `<option value="">Detect from GPS</option>` + projects.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("");
    sel.value = keep;
  }

  async function loadSiteOptions() {
    try {
      const { items } = await api("/api/sites");
      state.sites = items;
      const sel = $("#o-near");
      const keep = sel.value;
      sel.innerHTML = `<option value="">anywhere</option>` + items.map((s) => `<option value="${esc(s.site_id)}">${esc(s.site_name || s.site_id)}</option>`).join("");
      sel.value = keep;
    } catch (_) { /* ignore */ }
  }

  // ------------------------------------------------------------------- cards
  function card(item, extra = "") {
    const thumb = `/media/thumb/${encodeURIComponent(item.id)}`;
    const cloud = item.cloud || {};
    const verdict = cloud.integrity_level
      ? `<span class="pill lvl-${esc(cloud.integrity_level)} verdict" title="Checked by the NGO office">HQ: ${esc(cloud.integrity_level)} ${esc(cloud.integrity_score ?? "")}</span>` : "";
    const src = item.source === "mirror"
      ? `<span class="pill src-mirror src">From ${esc(item.device_name || item.device_id)}</span>`
      : `<span class="pill src-local src">This device</span>`;
    const tags = [...(cloud.ai_tags || []).map((t) => `<span class="tag ai">${esc(label(t))}</span>`),
                  ...(item.tags || []).filter((t) => !(cloud.ai_tags || []).includes(t)).map((t) => `<span class="tag">${esc(label(t))}</span>`)].slice(0, 4).join("");
    const reason = item.source === "local" && item.sync_reasons && item.sync_reasons.length ? `<div class="why">${esc(item.sync_reasons[0])}</div>` : "";
    const stateLabel = item.source === "local" ? `<span class="pill ${esc(item.sync_state)}">${esc(STATE_TEXT[item.sync_state] || item.sync_state)}</span>` : "";
    let why = "";
    if (item.why) {
      const bits = [];
      if (item.why.words && item.why.words.length) bits.push(`words: ${item.why.words.map(esc).join(", ")}`);
      if (item.why.semantic) bits.push("looks similar");
      if (item.distance_km != null) bits.push(`${Number(item.distance_km).toFixed(1)} km away`);
      if (bits.length) why = `<div class="whyhit">Matched on ${bits.join(" · ")}${item.score != null ? ` <span class="mono">(${Number(item.score).toFixed(3)})</span>` : ""}</div>`;
    }
    return `<article class="ev" tabindex="0" data-id="${esc(item.id)}">
      <div class="img" style="background-image:url('${thumb}')">${src}${verdict}</div>
      <div class="body">
        <div class="row">${stateLabel}${item.status_claim ? `<span class="tag">${esc(cap(item.status_claim))}</span>` : ""}</div>
        <div class="title" title="${esc(item.note || item.file_name)}">${esc(item.note || item.file_name || "Untitled")}</div>
        <div class="meta">${esc(item.site_name || "No site")} · ${esc(when(item.captured_at || item.captured_ts))}</div>
        ${tags ? `<div class="tags">${tags}</div>` : ""}
        ${why}${reason}${extra}
      </div>
    </article>`;
  }

  function bindCards(root) {
    $$(".ev", root).forEach((el) => {
      el.addEventListener("click", (e) => { if (!e.target.closest("[data-nope]")) openItem(el.dataset.id); });
      el.addEventListener("keydown", (e) => { if (e.key === "Enter") openItem(el.dataset.id); });
    });
  }

  // ------------------------------------------------------------------ memory
  async function loadMemory() {
    const { items } = await api("/api/evidence");
    const list = items.filter((i) => state.memFilter === "all" || i.source === state.memFilter);
    const grid = $("#memory-grid");
    grid.innerHTML = list.length ? list.map((i) => card(i)).join("")
      : `<div class="empty">No evidence yet. Drop photos in <b>Capture evidence</b>; they are saved and searchable on this device straight away, online or not.<br><br>Sample photos: <span class="mono">python scripts/seed_demo.py</span></div>`;
    bindCards(grid);
  }

  // ------------------------------------------------------------------ search
  function searchParams(q) {
    const p = new URLSearchParams({ q });
    const rec = $("#o-recency").value;
    if (rec) p.set("recency_hours", rec);
    const near = $("#o-near").value;
    if (near) { p.set("near_site", near); p.set("radius_km", $("#o-radius").value); if ($("#o-boost").checked) p.set("boost_near", "true"); }
    if ($("#o-status").value) p.set("status", $("#o-status").value);
    if ($("#o-source").value !== "all") p.set("source", $("#o-source").value);
    if ($("#o-diverse").checked) p.set("diverse", "true");
    if ($("#o-verified").checked) p.set("verified_only", "true");
    return p;
  }
  async function runSearch(q) {
    state.lastQuery = q;
    const r = await api(`/api/search?${searchParams(q)}`);
    const offline = state.status && state.status.sync.network_mode === "offline";
    const plan = $("#plan");
    plan.hidden = false;
    plan.innerHTML = `<div class="plan-head"><b>${r.latency_ms} ms</b> search${r.embed_ms != null ? ` + ${r.embed_ms} ms to embed the query` : ""} · ${esc(r.mode)} · ${r.results.length} result(s)${offline ? " · <span class=\"ok\">network is off</span>" : ""}</div>
      <ol class="steps">${(r.plan || []).map((s) => `<li>${esc(s)}</li>`).join("")}</ol>`;
    const grid = $("#search-grid");
    grid.innerHTML = r.results.length ? r.results.map((i) => card(i)).join("") : `<div class="empty">Nothing matched${q ? ` “${esc(q)}”` : ""} with these options.</div>`;
    bindCards(grid);
  }

  // --------------------------------------------------------------------- ask
  const ASK_CHIPS = ["What is the latest at the plantation strip?", "Any conflicts I should know about?", "What is still waiting to sync?",
    "What did HQ flag?", "What is near me?", "Show storm damage"];
  function renderChips() {
    $("#ask-chips").innerHTML = ASK_CHIPS.map((c) => `<button class="chip" type="button">${esc(c)}</button>`).join("");
    $$("#ask-chips .chip").forEach((b) => b.addEventListener("click", () => { $("#ask-q").value = b.textContent; ask(b.textContent); }));
  }
  async function ask(question) {
    if (!question) return;
    const chat = $("#chat");
    const id = `a${Date.now()}`;
    chat.insertAdjacentHTML("afterbegin", `<div class="qa" id="${id}"><div class="q">${esc(question)}</div><div class="a muted">Thinking on the device…</div></div>`);
    let lat = null; let lon = null;
    try {
      const r = await post("/api/ask", { question, lat, lon });
      const html = esc(r.answer).replace(/\[(\d+)\]/g, (_, n) => `<a href="#" class="cite" data-n="${n}">[${n}]</a>`).replace(/\n/g, "<br>");
      const cites = (r.citations || []).map((c) => `<li><a href="#" data-open="${esc(c.id)}">[${c.n}]</a> ${esc(c.label)} <span class="muted">· ${esc(c.site || "no site")} · ${esc(c.when)} · ${esc(c.who || "")}${c.hq ? ` · HQ ${esc(c.hq)}` : ""}</span></li>`).join("");
      $(`#${id} .a`).classList.remove("muted");
      $(`#${id} .a`).innerHTML = `<div>${html}</div>
        ${cites ? `<ol class="cites">${cites}</ol>` : ""}
        <div class="meta mono">${esc(r.intent)} · ${esc(r.engine)} · ${r.ms} ms · offline ✓</div>`;
      $$(`#${id} [data-open]`).forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); openItem(a.dataset.open); }));
      $$(`#${id} .cite`).forEach((a) => a.addEventListener("click", (e) => {
        e.preventDefault();
        const c = (r.citations || []).find((x) => String(x.n) === a.dataset.n);
        if (c) openItem(c.id);
      }));
    } catch (e) {
      $(`#${id} .a`).textContent = `Could not answer: ${e.message}`;
    }
  }

  // ------------------------------------------------------------------ outbox
  async function loadOutbox() {
    const [{ items }, s] = [await api("/api/outbox?include_done=true"), state.status];
    const lp = s && s.sync.last_pull;
    const reach = s ? s.sync.reachability : {};
    const counters = (s && s.sync.counters) || {};
    const link = (s && s.sync.link) || {};
    $("#sync-summary").innerHTML = [
      ["Link", link.quality ? cap(link.quality) : "unknown", link.kbps ? `${Math.round(link.kbps)} KB/s measured` : "measured from real transfers"],
      ["Qdrant server", reach.qdrant === null ? "Not configured" : (reach.qdrant ? "Reachable" : "Unreachable"), s ? `${s.sync.collection}${s.sync.layout ? ` · ${s.sync.layout} layout` : ""}` : ""],
      ["Impact (Cloudinary)", reach.impact === null ? "Not configured" : (reach.impact ? "Reachable" : "Unreachable"), s ? (s.sync.impact_url || "") : ""],
      ["Last pull", lp ? ago(lp.ts) : "Never", lp ? `${(lp.mode || "").split("+").map((m) => PULL_TEXT[m] || m).join(" + ")} · ${bytes(lp.bytes)} · ${lp.changed} changed` : "snapshot first, then only changes"],
      ["Sent / received", `${bytes(counters.bytes_uploaded)} / ${bytes(counters.bytes_downloaded)}`, "all syncs on this device so far"],
      ["Waiting", (s && ((s.outbox.queued || 0) + (s.outbox.failed || 0))) || 0, "most urgent first"],
    ].map(([k, v, sub]) => `<div class="sum"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div><div class="s">${esc(sub)}</div></div>`).join("");
    if (lp && lp.regions) {
      $("#last-pull").innerHTML = Object.entries(lp.regions).map(([k, r]) => `<div>${esc(k)}: ${esc(PULL_TEXT[r.mode] || r.mode || "")} · ${bytes(r.bytes)}${r.copied != null ? ` · ${r.copied} point(s)` : ""}${r.why ? ` · ${esc(r.why)}` : ""}${r.reason ? ` · ${esc(r.reason)}` : ""}</div>`).join("");
    }
    const tb = $("#outbox-table tbody");
    tb.innerHTML = items.length ? items.map((r) => `<tr>
        <td><a href="#" data-open="${esc(r.evidence_id)}">${esc(r.label || r.evidence_id.slice(0, 8))}</a><div class="mono">${esc(r.op)}</div></td>
        <td>${esc(cap(r.action))}</td>
        <td><span class="prio"><i style="width:${Math.max(4, r.priority)}%"></i></span><span class="mono">${r.priority}</span></td>
        <td>${esc(r.tier)}</td>
        <td>${statusCell(r)}</td>
        <td>${(r.reasons || []).map((x) => `<div>${esc(x)}</div>`).join("")}</td>
      </tr>`).join("") : `<tr><td colspan="6" class="hint">Nothing queued. Items appear here the moment they are captured.</td></tr>`;
    $$("[data-open]", tb).forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); openItem(a.dataset.open); }));
    if (!$("#regions").dataset.loaded) loadRegions();
  }
  function statusCell(r) {
    if (r.status === "done") return `<span class="pill synced">Sent</span>`;
    if (r.status === "failed") return `<span class="pill held">Retrying</span><div class="mono" title="${esc(r.last_error)}">${esc((r.last_error || "").slice(0, 60))}</div>`;
    const parts = [r.vectors_done ? "vectors ✓" : "vectors …", r.op === "push" ? (r.media_done ? "photo ✓" : "photo …") : ""].filter(Boolean);
    return `<span class="pill queued">Queued</span><div class="mono">${parts.join(" · ")}</div>`;
  }
  async function loadRegions() {
    const r = await api("/api/regions");
    const box = $("#regions");
    box.dataset.loaded = "1";
    box.innerHTML = r.available.map((p) => `<label><input type="checkbox" value="${esc(p.id)}" ${r.subscribed.includes(p.id) ? "checked" : ""}>
      ${esc(p.name)} <span class="muted small">${r.mirrors[p.id] != null ? `· ${r.mirrors[p.id]} on device` : ""}</span></label>`).join("");
  }
  $("#regions-save").addEventListener("click", async () => {
    const chosen = $$("#regions input:checked").map((i) => i.value);
    const r = await post("/api/regions", { regions: chosen });
    toast(`This device now serves ${r.subscribed.length} region(s). Mirrors update on the next pull.`);
    $("#regions").dataset.loaded = "";
    loadRegions();
  });

  // ------------------------------------------------------- sites + conflicts
  async function loadConflicts() {
    const { items } = await api("/api/conflicts");
    const list = $("#conflict-list");
    const open = items.filter((c) => c.state === "open");
    list.innerHTML = open.map((c) => `<div class="conflict">
        <div class="row"><strong>Conflict at ${esc(c.site_name || c.site_id)}</strong><span class="pill held">Needs a decision</span></div>
        <div class="claims">
          <div class="claim"><div class="who">This device (${esc(c.local_device)}) · ${esc(ago(c.local_ts))}</div><div class="st">${esc(label(c.local_status))}</div></div>
          <div class="claim"><div class="who">${esc(c.remote_device)} · ${esc(ago(c.remote_ts))}</div><div class="st">${esc(label(c.remote_status))}</div></div>
        </div>
        <div class="row">
          <button class="btn small primary" data-resolve="${c.id}" data-choice="local">Keep “${esc(label(c.local_status))}”</button>
          <button class="btn small" data-resolve="${c.id}" data-choice="remote">Accept “${esc(label(c.remote_status))}”</button>
        </div>
      </div>`).join("");
    $$("[data-resolve]", list).forEach((b) => b.addEventListener("click", async () => {
      await post(`/api/conflicts/${b.dataset.resolve}/resolve`, { choice: b.dataset.choice });
      toast("Decision saved. It syncs to the other devices first thing.");
      refreshAll();
    }));
  }
  async function loadSites() {
    await loadConflicts();
    const { items } = await api("/api/sites");
    state.sites = items;
    $("#sites").innerHTML = items.length ? items.map((s) => `<button class="site ${s.open_conflicts ? "has-conflict" : ""}" data-site="${esc(s.site_id)}" type="button">
        <div class="name">${esc(s.site_name || s.site_id)}</div>
        <div class="st st-${esc(s.status || "none")}">${esc(s.status ? cap(s.status) : "No status yet")}</div>
        <div class="muted small">${s.evidence} photo(s)${s.ts ? ` · ${esc(ago(s.ts))}` : ""}${s.device_id ? ` · ${esc(s.device_id)}` : ""}</div>
        ${s.open_conflicts ? `<span class="pill held">${s.open_conflicts} conflict</span>` : ""}
      </button>`).join("") : `<div class="empty">No sites yet. Photos with GPS are matched to project sites automatically.</div>`;
    $$("#sites .site").forEach((b) => b.addEventListener("click", () => openTimeline(b.dataset.site)));
    if (state.openSite) openTimeline(state.openSite, true);
  }
  async function openTimeline(siteId, quiet) {
    state.openSite = siteId;
    const t = await api(`/api/sites/${encodeURIComponent(siteId)}/timeline`);
    const box = $("#timeline");
    box.hidden = false;
    const ev = (t.events || []).slice().sort((a, b) => (b.ts || 0) - (a.ts || 0));
    box.innerHTML = `<div class="row between"><h3>${esc(t.site_name || siteId)} · history</h3><button class="btn small" id="tl-close" type="button">Close</button></div>
      ${t.current ? `<p class="hint">Current status: <b>${esc(cap(t.current.status))}</b>, reported by ${esc(((t.events || []).find((e) => e.device_id === t.current.device && e.who) || {}).who || t.current.device)} ${esc(ago(t.current.ts))}.</p>` : ""}
      <ol class="timeline">${ev.map((e) => `<li class="t-${esc(e.type)}">
        <div class="when mono">${esc(when(e.ts))}</div>
        <div class="what">
          ${e.type === "photo" ? `<img src="/media/thumb/${encodeURIComponent(e.id)}" alt="" onerror="this.remove()">` : ""}
          <div><b>${esc(e.type === "photo" ? "Photo" : e.type === "conflict" ? "Conflict" : "Decision")}</b>${e.status ? ` · <span class="tag">${esc(cap(e.status))}</span>` : ""}${e.hq ? ` <span class="pill lvl-${esc(e.hq)}">HQ ${esc(e.hq)}</span>` : ""}
            <div>${esc(e.text || "")}</div><div class="muted small">${esc(e.who || "")}${e.state ? ` · ${esc(e.state)}` : ""}</div>
            ${e.id ? `<a href="#" data-open="${esc(e.id)}" class="small">Open</a>` : ""}</div>
        </div></li>`).join("")}</ol>`;
    $("#tl-close").addEventListener("click", () => { box.hidden = true; state.openSite = null; });
    $$("[data-open]", box).forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); openItem(a.dataset.open); }));
    if (!quiet) box.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ---------------------------------------------------------------- activity
  async function loadActivity() {
    const { items } = await api("/api/activity?limit=150");
    $("#log").innerHTML = items.map((a) => `<li class="${esc(a.level)}"><span class="t">${esc(new Date(a.ts * 1000).toLocaleTimeString())}</span>
      <span class="k">${esc(a.kind)}</span><span>${esc(a.message)}</span></li>`).join("") || `<li><span></span><span></span><span class="hint">No activity yet.</span></li>`;
  }

  // ------------------------------------------------------------ system view
  async function loadSystem() {
    const s = await api("/api/system");
    const m = s.device.memory;
    const mirrors = Object.entries(m.mirrors || {});
    const deltas = m.delta_points || {};
    const srv = s.server || {};
    const sync = s.sync || {};
    const lp = sync.last_pull || {};
    const out = s.outbox || {};
    const mode = sync.network_mode;
    const linkCls = mode === "offline" || !sync.online ? "down" : mode === "slow" || (sync.link && sync.link.quality === "constrained") ? "slow" : "up";
    const regionBoxes = mirrors.length ? mirrors.map(([k, n]) => `<div class="shard mirror"><div class="k">Mirror · ${esc(k)}</div><div class="n">${n}</div><div class="s">${bytes((m.mirror_bytes || {})[k])}${deltas[k] ? ` · +${deltas[k]} delta` : ""}</div></div>`).join("")
      : `<div class="shard mirror empty"><div class="k">Mirrors</div><div class="s">none yet: pulled on first sync</div></div>`;
    const serverRegions = (srv.regions || []).map((r) => `<span class="tag ${(s.regions.subscribed || []).includes(r) ? "on" : ""}">${esc(r)}</span>`).join(" ");
    $("#sysmap").innerHTML = `
      <div class="node device">
        <div class="node-h">This device · ${esc(s.device.name)}</div>
        <div class="shards">
          <div class="shard local"><div class="k">Local shard</div><div class="n">${m.local_points}</div><div class="s">${m.local_indexed ? "HNSW indexed" : "exact search"} · ${m.local_segments} segment(s) · ${bytes(m.local_bytes)}</div></div>
          ${regionBoxes}
        </div>
        <div class="row small">
          <span class="tag">Outbox: ${(out.queued || 0) + (out.failed || 0)} waiting</span>
          <span class="tag">${esc(s.device.embedder)}</span>
          ${m.quantized ? `<span class="tag">int8 quantized</span>` : ""}
        </div>
      </div>
      <div class="link ${linkCls}">
        <div class="arrow up">↑ vectors first, then photos<br><span class="mono">${bytes((sync.counters || {}).bytes_uploaded)} sent</span></div>
        <div class="wire"><span></span></div>
        <div class="arrow down">↓ ${esc((lp.mode || "no pull yet").split("+").map((x) => PULL_TEXT[x] || x).join(" + "))}<br><span class="mono">${bytes((sync.counters || {}).bytes_downloaded)} received</span></div>
        <div class="linkstate">${linkCls === "down" ? "offline" : linkCls === "slow" ? "slow link" : "online"}</div>
      </div>
      <div class="node server">
        <div class="node-h">Qdrant server</div>
        ${srv.reachable ? `<div class="big">${srv.points ?? "?"} points</div><div class="small">${esc(srv.layout)} layout${srv.layout === "regional" ? " · one shard per region" : ""}</div><div class="tags">${serverRegions}</div>`
          : `<div class="muted">${srv.configured === false ? "not configured (device-only mode)" : "not reachable right now"}</div>`}
        <div class="node-h second">Impact · Cloudinary</div>
        <div class="small">${sync.reachability && sync.reachability.impact ? "reachable: stores photos, AI checks, before/after" : sync.reachability && sync.reachability.impact === null ? "not configured" : "not reachable right now"}</div>
      </div>`;
  }

  // -------------------------------------------------------------- benchmarks
  function svgBars(groups, series, opts = {}) {
    // groups: [label], series: [{name,color,values:[..]}]
    const W = 640; const H = 260; const P = { l: 46, r: 10, t: 16, b: 46 };
    const all = series.flatMap((s) => s.values.filter((v) => v != null));
    const log = !!opts.log;
    const max = Math.max(...all, 1);
    const min = log ? Math.max(Math.min(...all.filter((v) => v > 0)), 1) : 0;
    const y = (v) => {
      if (log) { const lv = Math.log10(Math.max(v, min)); const a = Math.log10(min) - 0.2; const b = Math.log10(max) + 0.15; return P.t + (H - P.t - P.b) * (1 - (lv - a) / (b - a)); }
      return P.t + (H - P.t - P.b) * (1 - v / (max * 1.12));
    };
    const gw = (W - P.l - P.r) / groups.length;
    const bw = Math.min(40, (gw * 0.8) / series.length);
    let s = `<svg viewBox="0 0 ${W} ${H}" class="chart" role="img" aria-label="${esc(opts.title || "")}">`;
    s += `<line x1="${P.l}" y1="${H - P.b}" x2="${W - P.r}" y2="${H - P.b}" class="axis"/>`;
    groups.forEach((g, gi) => {
      const x0 = P.l + gi * gw + (gw - bw * series.length) / 2;
      series.forEach((se, si) => {
        const v = se.values[gi];
        if (v == null) return;
        const yy = y(v);
        s += `<rect x="${x0 + si * bw}" y="${yy}" width="${bw - 3}" height="${Math.max(0, H - P.b - yy)}" fill="${se.color}"><title>${esc(se.name)}: ${esc(opts.fmt ? opts.fmt(v) : v)}</title></rect>`;
        s += `<text x="${x0 + si * bw + (bw - 3) / 2}" y="${yy - 4}" class="val">${esc(opts.fmt ? opts.fmt(v) : Math.round(v))}</text>`;
      });
      s += `<text x="${P.l + gi * gw + gw / 2}" y="${H - P.b + 16}" class="lab">${esc(g)}</text>`;
    });
    s += `<text x="12" y="${P.t + 4}" class="unit">${esc(opts.unit || "")}</text></svg>`;
    const legend = series.length > 1 ? `<div class="legend">${series.map((se) => `<span><i style="background:${se.color}"></i>${esc(se.name)}</span>`).join("")}</div>` : "";
    return s + legend;
  }
  function svgLines(xs, series, opts = {}) {
    const W = 640; const H = 280; const P = { l: 50, r: 12, t: 14, b: 40 };
    const lx = xs.map((x) => Math.log10(x));
    const ys = series.flatMap((s) => s.values.filter((v) => v != null && v > 0));
    const ya = Math.log10(Math.min(...ys)) - 0.1; const yb = Math.log10(Math.max(...ys)) + 0.1;
    const xa = Math.min(...lx) - 0.1; const xb = Math.max(...lx) + 0.1;
    const X = (x) => P.l + (W - P.l - P.r) * ((Math.log10(x) - xa) / (xb - xa));
    const Y = (v) => P.t + (H - P.t - P.b) * (1 - (Math.log10(v) - ya) / (yb - ya));
    let s = `<svg viewBox="0 0 ${W} ${H}" class="chart" role="img" aria-label="${esc(opts.title || "")}">`;
    s += `<line x1="${P.l}" y1="${H - P.b}" x2="${W - P.r}" y2="${H - P.b}" class="axis"/><line x1="${P.l}" y1="${P.t}" x2="${P.l}" y2="${H - P.b}" class="axis"/>`;
    for (let e = Math.ceil(ya); e <= Math.floor(yb); e++) {
      const v = Math.pow(10, e);
      s += `<line x1="${P.l}" x2="${W - P.r}" y1="${Y(v)}" y2="${Y(v)}" class="grid"/><text x="${P.l - 6}" y="${Y(v) + 4}" class="tick" text-anchor="end">${v >= 1 ? v : v.toFixed(1)} ms</text>`;
    }
    xs.forEach((x) => { s += `<text x="${X(x)}" y="${H - P.b + 16}" class="lab">${x >= 1000 ? `${x / 1000}k` : x}</text>`; });
    series.forEach((se) => {
      const pts = xs.map((x, i) => [x, se.values[i]]).filter((p) => p[1] != null && p[1] > 0);
      s += `<polyline fill="none" stroke="${se.color}" stroke-width="${se.bold ? 3 : 2}" points="${pts.map((p) => `${X(p[0])},${Y(p[1])}`).join(" ")}"/>`;
      pts.forEach((p) => { s += `<circle cx="${X(p[0])}" cy="${Y(p[1])}" r="3.5" fill="${se.color}"><title>${esc(se.name)} at ${p[0]}: ${p[1].toFixed(2)} ms</title></circle>`; });
    });
    s += `<text x="${W / 2}" y="${H - 6}" class="lab">photos in memory (log scale)</text></svg>`;
    return s + `<div class="legend">${series.map((se) => `<span><i style="background:${se.color}"></i>${esc(se.name)}</span>`).join("")}</div>`;
  }
  const C = { cloud: "#8a8f98", queue: "#d9a441", saakshi: "#0f8b6f", edge: "#0f8b6f", edge_int8: "#5cc2a6", edge_unindexed: "#9fcfc1", numpy: "#d9a441", server_http: "#6b7280" };
  const N = { cloud: "Cloud app", queue: "Offline queue app", saakshi: "Saakshi", edge: "Qdrant Edge (HNSW)", edge_int8: "Qdrant Edge int8",
    edge_unindexed: "Qdrant Edge, not indexed", numpy: "NumPy brute force", server_http: "Qdrant server over HTTP (localhost)" };

  async function loadBench() {
    if (!state.bench) {
      try { state.bench = await api("/api/benchmark/results"); } catch (_) { state.bench = { available: false }; }
    }
    const b = state.bench;
    if (!b || b.available === false) {
      $("#headlines").innerHTML = `<div class="empty">No benchmark results on this device yet. Run <span class="mono">python -m bench.run_all</span>, or the live test above.</div>`;
      return;
    }
    $("#headlines").innerHTML = (b.headlines || []).map((h) => `<div class="headline"><div class="v">${esc(h.value)}</div><div class="k">${esc(h.label)}</div><div class="s">${esc(h.detail)}</div></div>`).join("");
    const parts = [];
    if (b.search && b.search.sizes) {
      const rows = b.search.sizes.slice().sort((a, c) => a.n - c.n);
      const keys = ["edge", "edge_int8", "edge_unindexed", "numpy", "server_http"].filter((k) => rows.some((r) => r[k]));
      parts.push(`<figure><figcaption>Search latency as memory grows (p50, top-10, 512-d vectors)</figcaption>${svgLines(rows.map((r) => r.n), keys.map((k) => ({ name: N[k], color: C[k], bold: k === "edge", values: rows.map((r) => (r[k] ? r[k].p50_ms : null)) })))}
        <table class="table compact"><thead><tr><th>Photos</th>${keys.map((k) => `<th>${esc(N[k])}</th>`).join("")}</tr></thead><tbody>
        ${rows.map((r) => `<tr><td>${r.n.toLocaleString()}</td>${keys.map((k) => `<td>${r[k] ? `${r[k].p50_ms.toFixed(2)} ms · recall ${r[k].recall_at_10.toFixed(2)}` : "–"}</td>`).join("")}</tr>`).join("")}</tbody></table></figure>`);
    }
    const reg = b.sync && (b.sync.regional || b.sync.global);
    // What was measured when nothing changed: some servers answer 304, Qdrant Cloud re-sends the region.
    // The device itself counts changes first and downloads nothing in that case.
    const noChangeText = (r) => {
      const nc = r.no_change || {};
      return nc.status === 304 || !nc.bytes ? "No change: HTTP 304, 0 bytes."
        : `No change: this server re-sends the region in a partial snapshot (${bytes(nc.bytes)}), so the device counts changes first and downloads nothing.`;
    };
    if (reg && reg.one_point) {
      const items = [["Full region", reg.first_pull.bytes], ["Re-download all", reg.naive_repull.bytes], ["Partial, 50 new", reg.incremental_pull.bytes],
        ["Delta, 50 new", reg.incremental_delta.bytes], ["Partial, 1 new", reg.one_point.partial.bytes], ["Delta, 1 new", reg.one_point.delta.bytes]];
      parts.push(`<figure><figcaption>What a device downloads to stay in sync with its region (log scale)</figcaption>${svgBars(items.map((i) => i[0]), [{ name: "bytes", color: C.saakshi, values: items.map((i) => i[1]) }], { log: true, fmt: bytes, unit: "bytes" })}
        <p class="small muted">${noChangeText(reg)} ${b.sync.regional && b.sync.global ? `Whole collection (${b.sync.regional.projects} regions): ${bytes(b.sync.global.first_pull.bytes)}; one region: ${bytes(b.sync.regional.first_pull.bytes)}.` : ""}</p></figure>`);
    }
    if (b.fieldday) {
      const f = b.fieldday;
      const scen = Object.keys(f.scenarios);
      const labs = scen.map((k) => f.scenarios[k].label.split(" (")[0]);
      const metric = state.benchMetric || "team_recall_pct";
      const M = { team_recall_pct: ["Team evidence found when searching", "%"], query_answered_pct: ["Searches answered", "%"], mb_up_slow_per_day: ["Data through 2G per team-day", "MB"],
        urgent_photo_min_p90: ["Urgent photo at HQ, 90th percentile", "min"], faces_unblurred_per_day: ["Faces uploaded unblurred per team-day", "count"], dup_uploads_per_day: ["Repeat shots uploaded per team-day", "count"] };
      parts.push(`<figure><figcaption>A simulated field day, ${f.scenarios[scen[0]].saakshi.days} days per place, same photos and network for all three</figcaption>
        <div class="seg" id="metric">${Object.entries(M).map(([k, v]) => `<button data-m="${k}" class="${k === metric ? "on" : ""}">${esc(v[0])}</button>`).join("")}</div>
        ${svgBars(labs, ["cloud", "queue", "saakshi"].map((st) => ({ name: N[st], color: C[st], values: scen.map((k) => f.scenarios[k][st][metric]) })), { unit: M[metric][1], fmt: (v) => (v >= 10 ? Math.round(v) : v.toFixed(1)) })}
        <p class="small muted">Policy decisions come from the production code (field/app/policy.py); snapshot and delta sizes from the sync benchmark. Assumptions are listed in bench/bench_fieldday.py.</p></figure>`);
    }
    $("#charts").innerHTML = parts.join("");
    $$("#metric button").forEach((btn) => btn.addEventListener("click", () => { state.benchMetric = btn.dataset.m; loadBench(); }));
    const mach = (b.search && b.search.machine) || {};
    $("#bench-meta").textContent = `Generated ${b.generated || ""}${mach.processor ? ` on ${mach.processor}, ${mach.cpus} CPU(s), Python ${mach.python}` : ""}.`;
  }
  $("#live-bench").addEventListener("click", async () => {
    const btn = $("#live-bench");
    btn.disabled = true; btn.textContent = "Running…";
    try {
      const r = await post("/api/benchmark/live");
      const box = $("#live-result");
      box.hidden = false;
      const srv = r.server || {};
      box.innerHTML = `<div class="live-grid">
        <div><div class="k">On this device (Qdrant Edge)</div><div class="v">${r.device.p50_ms} ms</div><div class="s">p50 of ${r.queries} searches · p95 ${r.device.p95_ms} ms · ${r.device.points} points in ${r.device.shards} shard(s) · works offline</div></div>
        <div><div class="k">On the server</div><div class="v">${srv.reachable ? `${srv.p50_ms} ms` : "unreachable"}</div><div class="s">${srv.reachable ? `p50 · p95 ${srv.p95_ms} ms · ${srv.points} points · over the network` : esc(srv.note || srv.error || "")}</div></div>
        ${r.speedup_p50 ? `<div><div class="k">Device vs server</div><div class="v">${r.speedup_p50}×</div><div class="s">faster at p50, and it does not need the network at all</div></div>` : ""}
      </div>`;
    } catch (e) { toast(e.message); }
    btn.disabled = false; btn.textContent = "Run live test on this device";
  });

  // ------------------------------------------------------------------ drawer
  async function openItem(id) {
    let item;
    try { item = await api(`/api/evidence/${encodeURIComponent(id)}`); } catch (e) { toast(e.message); return; }
    const cloud = item.cloud || {};
    const local = item.source === "local";
    const statusOpts = ['<option value="">No status</option>'].concat((state.status?.status_claims || []).map((c) => `<option value="${c}" ${item.status_claim === c ? "selected" : ""}>${cap(c)}</option>`)).join("");
    state.excluded[id] = state.excluded[id] || [];
    $("#drawer-content").innerHTML = `
      <h2 id="d-title">${esc(item.note || item.file_name || "Evidence")}</h2>
      <img src="/media/thumb/${encodeURIComponent(item.id)}" alt="Photo" onerror="this.style.display='none'">
      <div class="row" style="margin-top:10px">
        ${local ? `<span class="pill ${esc(item.sync_state)}">${esc(STATE_TEXT[item.sync_state] || item.sync_state)}</span>` : `<span class="pill src-mirror">From ${esc(item.device_name || item.device_id)}</span>`}
        ${item.status_claim ? `<span class="tag">${esc(cap(item.status_claim))}</span>` : ""}
        ${item.media_evicted ? `<span class="tag">Original released from device (safe in the cloud)</span>` : ""}
      </div>
      ${local && item.sync_reasons ? `<div class="box"><h3>Why the device decided this</h3><ul class="reasons">${item.sync_reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
        ${item.sync_state === "held" ? `<div class="row"><button class="btn small primary" data-approve="blur">Upload with faces blurred</button><button class="btn small" data-approve="consent">Consent recorded, upload as is</button></div>` : ""}
      </div>` : ""}
      ${cloud.integrity_level ? `<div class="box hq"><h3>Checked by the NGO office</h3>
        <div class="row"><span class="pill lvl-${esc(cloud.integrity_level)}">${esc(cloud.integrity_level)} · ${esc(cloud.integrity_score)}</span>
        ${cloud.vision_source && cloud.vision_source !== "none" ? `<span class="mono">${esc(cloud.vision_source)}</span>` : ""}</div>
        ${cloud.caption ? `<p>${esc(cloud.caption)}</p>` : ""}
        ${(cloud.integrity_reasons || []).length ? `<ul class="reasons">${cloud.integrity_reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
        <div class="tags">${(cloud.ai_tags || []).map((t) => `<span class="tag ai">${esc(label(t))}</span>`).join("")}</div></div>` : ""}
      <dl class="kv">
        <dt>Site</dt><dd>${esc(item.site_name || "—")}</dd>
        <dt>Project</dt><dd>${esc(item.project_name || item.project_id || "—")}</dd>
        <dt>Taken</dt><dd>${esc(when(item.captured_at))}</dd>
        <dt>Location</dt><dd>${item.lat != null ? `${Number(item.lat).toFixed(5)}, ${Number(item.lon).toFixed(5)}` : "No GPS"}</dd>
        <dt>Faces detected</dt><dd>${esc(item.faces ?? 0)}</dd>
        <dt>On-device tags</dt><dd>${(item.tags || []).map(label).join(", ") || "—"}</dd>
        <dt>Captured by</dt><dd>${esc(item.device_name || item.device_id)}</dd>
        <dt>Fingerprint</dt><dd class="mono">${esc((item.sha256 || "").slice(0, 16))}… · dHash ${esc(item.dhash || "")}</dd>
        <dt>Version</dt><dd>${esc(item.version ?? 1)}</dd>
      </dl>
      ${local ? `<div class="box"><h3>Edit</h3>
        <div class="field"><label for="e-note">Note</label><textarea id="e-note" rows="2">${esc(item.note || "")}</textarea></div>
        <div class="field"><label for="e-status">Site status</label><select id="e-status">${statusOpts}</select></div>
        <div class="checks"><label><input type="checkbox" id="e-private" ${item.private ? "checked" : ""}> Private (never leaves this device)</label>
        <label><input type="checkbox" id="e-consent" ${item.consent ? "checked" : ""}> Consent recorded for people shown</label></div>
        <button class="btn primary small" id="e-save">Save changes</button></div>` : ""}
      <div class="box"><h3>Visually similar in memory</h3><div class="grid small-grid" id="similar"></div></div>
      <div class="box"><h3>More like this <span class="muted small">(Qdrant recommend: mark results “not this” to steer)</span></h3><div class="grid small-grid" id="mlt"></div></div>`;
    const d = $("#drawer");
    d.hidden = false;
    $$("[data-approve]").forEach((b) => b.addEventListener("click", async () => {
      await post(`/api/evidence/${encodeURIComponent(id)}/approve`, { mode: b.dataset.approve });
      toast("Released for upload"); closeDrawer(); refreshAll();
    }));
    const save = $("#e-save");
    if (save) save.addEventListener("click", async () => {
      try {
        await patch(`/api/evidence/${encodeURIComponent(id)}`, {
          note: $("#e-note").value, status_claim: $("#e-status").value || null,
          private: $("#e-private").checked, consent: $("#e-consent").checked,
        });
        toast("Saved on the device"); closeDrawer(); refreshAll();
      } catch (e) { toast(e.message); }
    });
    try {
      const { items } = await api(`/api/evidence/${encodeURIComponent(id)}/similar`);
      $("#similar").innerHTML = items.length ? items.slice(0, 4).map((i) => card(i)).join("") : `<p class="hint">Nothing similar yet.</p>`;
      bindCards($("#similar"));
    } catch (_) { /* ignore */ }
    loadMoreLikeThis(id);
  }
  async function loadMoreLikeThis(id) {
    try {
      const ex = state.excluded[id] || [];
      const { items } = await api(`/api/evidence/${encodeURIComponent(id)}/more-like-this?exclude=${encodeURIComponent(ex.join(","))}`);
      const box = $("#mlt");
      if (!box) return;
      box.innerHTML = items.length ? items.slice(0, 6).map((i) => card(i, `<button class="btn small nope" data-nope="${esc(i.id)}" type="button">Not this</button>`)).join("") : `<p class="hint">Nothing else yet.</p>`;
      bindCards(box);
      $$("[data-nope]", box).forEach((b) => b.addEventListener("click", (e) => {
        e.stopPropagation();
        state.excluded[id] = [...(state.excluded[id] || []), b.dataset.nope];
        loadMoreLikeThis(id);
      }));
    } catch (_) { /* ignore */ }
  }
  function closeDrawer() { $("#drawer").hidden = true; }
  $$("[data-close]").forEach((el) => el.addEventListener("click", closeDrawer));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeDrawer(); endTour(); } });

  // ------------------------------------------------------------------ capture
  const drop = $("#drop");
  const fileInput = $("#files");
  function setPicked(files) {
    state.pickedFiles = Array.from(files).filter((f) => f.type.startsWith("image/") || /\.(jpe?g|png|webp|heic)$/i.test(f.name));
    $("#picked").textContent = state.pickedFiles.length ? `${state.pickedFiles.length} photo(s) ready` : "";
    $("#import").disabled = !state.pickedFiles.length;
  }
  fileInput.addEventListener("change", () => setPicked(fileInput.files));
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => setPicked(e.dataTransfer.files));

  $("#import").addEventListener("click", async () => {
    const btn = $("#import");
    btn.disabled = true;
    btn.textContent = "Saving…";
    const fd = new FormData();
    state.pickedFiles.forEach((f) => fd.append("files", f, f.name));
    fd.append("note", $("#note").value);
    fd.append("project_id", $("#project").value);
    fd.append("status_claim", $("#status").value);
    fd.append("consent", $("#consent").checked);
    fd.append("private", $("#private").checked);
    try {
      const { results } = await api("/api/evidence", { method: "POST", body: fd });
      $("#import-result").innerHTML = results.map((r) => r.error
        ? `<div class="r bad">${esc(r.file_name)}: ${esc(r.error)}</div>`
        : r.duplicate_file ? `<div class="r">${esc(r.file_name)}: already in memory</div>`
        : `<div class="r"><b>${esc(r.file_name)}</b> → ${esc(r.site_name || "no site")} · ${esc(cap(r.sync_action))} (priority ${r.priority}) · ${r.ingest_ms} ms<br>${esc((r.sync_reasons || [])[0] || "")}${r.claim && r.claim.kind === "conflict" ? `<br><b>Conflict:</b> ${esc(r.claim.message)}` : ""}</div>`).join("");
      setPicked([]);
      fileInput.value = "";
      $("#note").value = "";
      refreshAll();
      loadSiteOptions();
    } catch (e) {
      toast(`Could not save: ${e.message}`);
    } finally {
      btn.textContent = "Save to device memory";
      btn.disabled = !state.pickedFiles.length;
    }
  });

  // ------------------------------------------------------------------ policy
  $("#dup").addEventListener("input", () => { $("#dup-val").textContent = `${Math.round($("#dup").value * 100)}%`; });
  $("#dup").addEventListener("change", () => post("/api/settings", { duplicate_threshold: Number($("#dup").value) }));
  $("#budget").addEventListener("input", () => { $("#budget-val").textContent = bytes($("#budget").value * 1024 * 1024); });
  $("#budget").addEventListener("change", async () => {
    await post("/api/settings", { media_budget_mb: Number($("#budget").value) });
    toast("Storage budget saved. Only originals already safe in the cloud are ever released.");
    refreshStatus();
  });
  $("#privacy").addEventListener("change", () => post("/api/settings", { privacy_mode: $("#privacy").value }).then(() => toast("Applies to new photos")));
  $("#metered").addEventListener("change", () => post("/api/settings", { metered: $("#metered").checked }));
  $("#autobw").addEventListener("change", () => post("/api/settings", { auto_bandwidth: $("#autobw").checked }));
  $("#autosync").addEventListener("change", () => post("/api/settings", { auto_sync: $("#autosync").checked }));

  // ------------------------------------------------------------- top actions
  $$("#net-mode button").forEach((b) => b.addEventListener("click", async () => {
    const mode = b.dataset.mode;
    await post("/api/network", { mode });
    toast({ offline: "Network off. Capture, search and Ask keep working.", slow: "Simulated 2G: urgent photos first, routine photos wait, vectors still go.", online: "Network on. Syncing the queue." }[mode]);
    await refreshAll();
  }));
  $("#sync-now").addEventListener("click", async () => {
    const btn = $("#sync-now");
    btn.disabled = true; btn.textContent = "Syncing…";
    try {
      const r = await post("/api/sync/now");
      if (!r.ok) toast(r.message);
      else toast(`Sent ${r.push.vectors} point(s), ${r.push.media} photo(s)${r.push.deferred ? `, ${r.push.deferred} waiting for a better link` : ""}. Pulled ${r.pull.pulled ? `${(r.pull.mode || "").split("+").map((m) => PULL_TEXT[m] || m).join(" + ")} (${bytes(r.pull.bytes)}), ${r.pull.changed} change(s)` : `nothing (${r.pull.reason || ""})`}`);
    } catch (e) { toast(e.message); }
    btn.disabled = false; btn.textContent = "Sync now";
    refreshAll();
  });

  // -------------------------------------------------------------------- tabs
  function showTab(name) {
    state.tab = name;
    $$(".tabs button").forEach((x) => {
      const on = x.dataset.tab === name;
      x.classList.toggle("active", on);
      x.setAttribute("aria-selected", String(on));
      if (on) x.scrollIntoView({ block: "nearest", inline: "nearest" });  // narrow screens scroll the tab row
    });
    $$(".panel").forEach((p) => { p.hidden = p.id !== `tab-${name}`; });
    refreshTab();
    if (name === "search") $("#q").focus();
    if (name === "ask") $("#ask-q").focus();
  }
  $$(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
  // Fade the right edge while more tabs are hidden, so a phone user knows the row scrolls
  const tabRow = $(".tabs");
  const tabFade = () => tabRow.classList.toggle("more", tabRow.scrollLeft + tabRow.clientWidth < tabRow.scrollWidth - 4);
  tabRow.addEventListener("scroll", tabFade, { passive: true });
  window.addEventListener("resize", tabFade);
  tabFade();
  $$("#mem-filter button").forEach((b) => b.addEventListener("click", () => {
    state.memFilter = b.dataset.f;
    $$("#mem-filter button").forEach((x) => x.classList.toggle("on", x === b));
    loadMemory();
  }));
  $("#search-form").addEventListener("submit", (e) => { e.preventDefault(); runSearch($("#q").value.trim()); });
  $$("#search-opts select, #search-opts input").forEach((el) => el.addEventListener("change", () => { if (state.lastQuery !== null) runSearch(state.lastQuery); }));
  $("#ask-form").addEventListener("submit", (e) => { e.preventDefault(); ask($("#ask-q").value.trim()); });

  async function refreshTab() {
    try {
      if (state.tab === "memory") await loadMemory();
      else if (state.tab === "search") { if (state.lastQuery !== null) await runSearch(state.lastQuery); }
      else if (state.tab === "outbox") await loadOutbox();
      else if (state.tab === "sites") await loadSites();
      else if (state.tab === "system") await loadSystem();
      else if (state.tab === "bench") await loadBench();
      else if (state.tab === "activity") await loadActivity();
    } catch (e) { /* keep the last view if a refresh fails */ }
  }
  async function refreshAll() { await refreshStatus(); await refreshTab(); }

  // -------------------------------------------------------------------- tour
  const TOUR = [
    { el: "#capture", tab: "memory", title: "Capture evidence", text: "Drop a photo. In a few hundred milliseconds it is embedded, matched to a project site from its GPS, checked for faces and repeats, and stored in this device's Qdrant Edge shard." },
    { el: "#net-mode", title: "Take the network away", text: "Switch to Offline or 2G at any time. Capture, search and Ask keep working because they never leave the device. On 2G, urgent photos go first and routine photos wait." },
    { el: "#tab-search", tab: "search", title: "Search on the device", text: "Hybrid search (picture + words) with recency, distance and diversity options. The query plan shows exactly what Qdrant Edge ran and how long it took." },
    { el: "#tab-ask", tab: "ask", title: "Ask in plain words", text: "Questions like “what is the latest at the plantation strip?” are answered from the device's memory, with a citation to every photo used." },
    { el: "#tab-outbox", tab: "outbox", title: "Sync what matters first", text: "Every item carries the reason for its sync decision. Vectors go first, then photos by priority. Pulls download only the device's regions, and only what changed." },
    { el: "#tab-sites", tab: "sites", title: "One site, two reports", text: "When two officers report the same site differently, the conflict appears here once. Whoever decides, the decision syncs to every device, and newer evidence closes it on its own." },
    { el: "#tab-system", tab: "system", title: "See the whole system", text: "A live map of the shards on this device, the link, and the regional collection on the server." },
    { el: "#tab-bench", tab: "bench", title: "Measured, not claimed", text: "Benchmarks against brute force and a server, a simulated field day in three kinds of places, and a live test you can run here." },
  ];
  let tourAt = -1;
  function tourShow() {
    $$(".tour-hi").forEach((x) => x.classList.remove("tour-hi"));
    const step = TOUR[tourAt];
    if (!step) { endTour(); return; }
    if (step.tab) showTab(step.tab);
    const el = $(step.el);
    if (el) { el.classList.add("tour-hi"); el.scrollIntoView({ behavior: "smooth", block: "center" }); }
    $("#tour").hidden = false;
    $("#tour-step").textContent = `${tourAt + 1} / ${TOUR.length}`;
    $("#tour-title").textContent = step.title;
    $("#tour-text").textContent = step.text;
    $("#tour-next").textContent = tourAt === TOUR.length - 1 ? "Done" : "Next";
  }
  function endTour() {
    tourAt = -1;
    $("#tour").hidden = true;
    $$(".tour-hi").forEach((x) => x.classList.remove("tour-hi"));
    store.set("saakshi-tour-seen", "1");
  }
  $("#tour-btn").addEventListener("click", () => { tourAt = 0; tourShow(); });
  $("#tour-next").addEventListener("click", () => { tourAt += 1; if (tourAt >= TOUR.length) endTour(); else tourShow(); });
  $("#tour-skip").addEventListener("click", endTour);

  // -------------------------------------------------------------------- boot
  renderChips();
  loadProjects().catch(() => {});
  loadSiteOptions();
  refreshAll();
  if (!store.get("saakshi-tour-seen")) setTimeout(() => { if (tourAt < 0) { tourAt = 0; tourShow(); } }, 900);
  setInterval(() => {
    if (!$("#drawer").hidden || document.visibilityState !== "visible" || tourAt >= 0) return;
    if (state.tab === "bench" || state.tab === "ask") refreshStatus(); else refreshAll();
  }, 4000);
  setInterval(() => { loadProjects().catch(() => {}); loadSiteOptions(); }, 60000);
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => navigator.serviceWorker.register("sw.js").catch(() => {}));
  }
})();
