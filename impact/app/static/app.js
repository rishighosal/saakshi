/* Saakshi Impact: dashboard logic. Hash router, plain JavaScript. */
(() => {
  "use strict";

  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const view = $("#view");
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const lbl = (s) => String(s || "").replace(/_/g, " ");
  const cap = (s) => { s = lbl(s); return s.charAt(0).toUpperCase() + s.slice(1); };
  const LEVEL = { verified: "Verified", review: "Needs review", flagged: "Flagged" };
  const S = { status: null, map: null };

  // Changing evidence, uploading and campaign images need the office key (the server's
  // INGEST_TOKEN). It is asked for once and kept in this browser only.
  const KEY = "saakshi.officeKey";
  const store = {
    get() { try { return localStorage.getItem(KEY) || ""; } catch (_) { return ""; } },
    set(v) { try { v ? localStorage.setItem(KEY, v) : localStorage.removeItem(KEY); } catch (_) { /* storage blocked */ } },
  };
  function askKey(wrong) {
    const d = $("#keydlg");
    const f = $("#keyform");
    $("#key-wrong").hidden = !wrong;
    f.reset();
    d.returnValue = "";
    $("#key-cancel").onclick = () => d.close("");
    return new Promise((resolve) => {
      d.addEventListener("close", () => resolve(d.returnValue === "ok" ? f.key.value.trim() : ""), { once: true });
      d.showModal();
    });
  }

  async function api(path, opts = {}, retried = false) {
    let key = store.get();
    if (!key && opts.method && opts.method !== "GET" && S.status && S.status.office_key_required) {
      key = await askKey(false);  // ask before sending, so the request is not refused first
      if (!key) throw new Error("This action needs the office key");
      store.set(key);
    }
    const headers = { ...(opts.headers || {}), ...(key ? { "X-Saakshi-Token": key } : {}) };
    const r = await fetch(path, { ...opts, headers });
    if (r.status === 401) {
      store.set("");  // a refused key is never sent again
      if (retried) throw new Error("That office key was not accepted");
      const k = await askKey(Boolean(key));
      if (!k) throw new Error("This action needs the office key");
      store.set(k);
      return api(path, opts, true);
    }
    if (!r.ok) {
      let m = r.statusText;
      try { m = (await r.json()).detail || m; } catch (_) { /* not json */ }
      throw new Error(m);
    }
    return r.json();
  }
  const post = (p, b) => api(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b || {}) });
  const patch = (p, b) => api(p, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b || {}) });
  function toast(m) { const t = $("#toast"); t.textContent = m; t.hidden = false; clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), 3500); }
  function day(iso) { if (!iso) return "—"; const d = new Date(iso); return isNaN(d) ? iso : d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }); }
  function dt(iso) { if (!iso) return "—"; const d = new Date(iso); return isNaN(d) ? iso : d.toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }); }
  function tsTime(ts) { const d = new Date(ts * 1000); return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }); }
  const levelPill = (a) => a.integrity_level ? `<span class="pill ${esc(a.integrity_level)}">${esc(LEVEL[a.integrity_level] || a.integrity_level)} · ${esc(a.integrity_score)}</span>` : "";
  const firstProblem = (a) => ((a.integrity && a.integrity.checks) || []).find((c) => c.status === "fail") || ((a.integrity && a.integrity.checks) || []).find((c) => c.status === "warn");

  // ---------------------------------------------------------------- public demo
  // A public deployment is read-only: visitors see every result, but nothing that writes
  // (and could spend the AI Vision quota). Controls that write carry data-write.
  function demoLinks(demo) {
    const video = demo.video_url ? `<a href="${esc(demo.video_url)}" target="_blank" rel="noopener">watch the video</a>, or ` : "";
    const repo = demo.repo_url ? `<a href="${esc(demo.repo_url)}#run-it" target="_blank" rel="noopener">run it on your own machine</a>` : "run it on your own machine";
    return `To try uploads, ${video}${repo}.`;
  }
  function showDemoNote(demo) {
    document.body.classList.toggle("ro", Boolean(demo));
    const n = $("#demo-note");
    if (!demo) { n.hidden = true; return; }
    n.innerHTML = `This public demo is read-only to protect the AI quota. ${demoLinks(demo)}`;
    n.hidden = false;
  }
  const readOnly = () => Boolean(S.status && S.status.public_demo);

  // Campaign images already made (shown instead of making new ones in the public demo)
  function campaignGallery(items) {
    $("#modal-content").innerHTML = `<h2 id="m-title">Campaign images</h2>
      <p class="muted">Rendered by Cloudinary from the original: cropped around the subject for each format, colours improved, faces blurred unless consent is recorded, caption and credit added.</p>
      <div class="variants">${items.map((v) => `<div class="variant"><div class="ph"><img src="${esc(v.url)}" alt="${esc(v.name)}" loading="lazy"></div>
        <div class="b"><b>${esc(v.name)}</b><details><summary>How it was made</summary><ol class="steps">${v.steps.map((s) => `<li>${esc(s)}</li>`).join("")}</ol></details></div></div>`).join("")}</div>`;
    $("#modal").hidden = false;
  }

  // ---------------------------------------------------------------- status
  async function loadStatus() {
    try {
      S.status = await api("/api/status");
      $("#org").textContent = S.status.org;
      showDemoNote(S.status.public_demo);
      const b = $("#banner");
      if (S.status.store !== "cloudinary") {
        b.hidden = false;
        b.innerHTML = "Cloudinary is not configured, so photos are stored locally and transformations, face blurring and AI Vision are off. Add <span class='mono'>CLOUDINARY_URL</span> to <span class='mono'>.env</span> and restart.";
      } else if (S.status.ai_vision_error) {
        b.hidden = false;
        b.innerHTML = `AI Vision is not answering (${esc(S.status.ai_vision_error.slice(0, 160))}). Tags fall back to on-server CLIP. Enable the AI Vision add-on in the Cloudinary console.`;
      } else b.hidden = true;
    } catch (_) { /* server restarting */ }
  }

  // ---------------------------------------------------------------- cards
  function evCard(a) {
    const prob = a.integrity_level !== "verified" ? firstProblem(a) : null;
    const tags = (a.ai_tags || []).slice(0, 3).map((t) => `<span class="tag ai">${esc(lbl(t))}</span>`).join("");
    return `<a class="ev" href="#/a/${esc(a.id)}">
      <div class="img" style="background-image:url('${esc(pub(a))}')">
        <span class="lv">${levelPill(a)}</span>
        ${a.source === "field" ? `<span class="pill dark src" title="Synced from a field device">${esc(a.device_name || "Field device")}</span>` : ""}
        ${a.media_type === "video" ? `<span class="pill dark vid">▶ Video${a.duration ? ` · ${Math.round(a.duration)} s` : ""}</span>` : ""}
      </div>
      <div class="body">
        <div class="title">${esc(a.note || a.caption || "Field photo")}</div>
        <div class="meta">${esc(a.site_name || "No site")} · ${esc(day(a.captured_at || a.uploaded_at))}</div>
        ${tags ? `<div class="tags">${tags}</div>` : ""}
        ${prob ? `<div class="why">${esc(prob.message)}</div>` : ""}
      </div></a>`;
  }

  // ---------------------------------------------------------------- overview
  const ICON = {
    check: '<path d="M20 6 9 17l-5-5"/>',
    pin: '<path d="M12 21s-7-6.2-7-11a7 7 0 0 1 14 0c0 4.8-7 11-7 11z"/><circle cx="12" cy="10" r="2.5"/>',
    folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
    face: '<circle cx="12" cy="12" r="9"/><path d="M9 10h.01M15 10h.01M8.5 15c1 1 2.2 1.5 3.5 1.5s2.5-.5 3.5-1.5"/><path d="M4 4l16 16"/>',
    tag: '<path d="M3 12V4a1 1 0 0 1 1-1h8l9 9-9 9z"/><circle cx="7.5" cy="7.5" r="1.5"/>',
    phone: '<rect x="7" y="2" width="10" height="20" rx="2"/><path d="M11 18h2"/><path d="M3 9l2 2M21 9l-2 2"/>',
    sync: '<path d="M4 12a8 8 0 0 1 14-5.3L20 9"/><path d="M20 4v5h-5"/><path d="M20 12a8 8 0 0 1-14 5.3L4 15"/><path d="M4 20v-5h5"/>',
    shield: '<path d="M12 3 4 6v6c0 5 3.4 8.4 8 9 4.6-.6 8-4 8-9V6z"/><path d="m9 12 2 2 4-4"/>',
    report: '<path d="M6 3h9l4 4v14H6z"/><path d="M14 3v5h5"/><path d="M9 13h6M9 17h6"/>',
  };
  const icon = (k) => `<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${ICON[k]}</svg>`;
  const reduced = () => Boolean(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  function countUp(root) {
    $$("[data-count]", root).forEach((el) => {
      const n = Number(el.dataset.count);
      if (reduced() || !n) { el.textContent = n; return; }
      const t0 = performance.now();
      const step = (t) => { const k = Math.min(1, (t - t0) / 900); el.textContent = Math.round(n * (1 - Math.pow(1 - k, 3))); if (k < 1) requestAnimationFrame(step); };
      requestAnimationFrame(step);
    });
  }
  // Public pages show the face-blurred rendition
  const pub = (a) => (readOnly() ? a.public_thumb_url : a.thumb_url) || a.thumb_url;
  function galleryCard(a) {
    const tags = (a.ai_tags || []).slice(0, 3).map((t) => `<span class="tag ai">${esc(lbl(t))}</span>`).join("");
    return `<button type="button" class="gcard" data-lightbox="${esc(a.id)}">
      <span class="ph skel"><img src="${esc(pub(a))}" alt="${esc(a.caption || a.note || "Field photo")}" loading="lazy"><span class="lv">${levelPill(a)}</span></span>
      <span class="b"><span class="title">${esc(a.note || a.caption || "Field photo")}</span>
        <span class="meta">${esc(a.site_name || "No location")} · ${esc(day(a.captured_at || a.uploaded_at))}</span>
        ${tags ? `<span class="tags">${tags}</span>` : ""}</span></button>`;
  }
  function lightbox(a) {
    const checks = ((a.integrity && a.integrity.checks) || []).filter((c) => c.status !== "pass").slice(0, 3);
    $("#modal-content").innerHTML = `<figure class="lightbox">
      <img src="${esc((readOnly() ? a.public_display_url : a.display_url) || pub(a))}" alt="${esc(a.caption || a.note || "Field photo")}">
      <figcaption><div class="row">${levelPill(a)}<span class="muted">${esc(a.project_name || "")} · ${esc(a.site_name || "No location")} · ${esc(day(a.captured_at))}</span></div>
        <h2 id="m-title">${esc(a.note || "Field photo")}</h2>
        ${a.caption ? `<p class="muted"><b>AI Vision caption:</b> ${esc(a.caption)}</p>` : ""}
        ${checks.length ? `<ul class="checks-mini">${checks.map((c) => `<li class="${esc(c.status)}">${esc(c.message)}</li>`).join("")}</ul>` : ""}
        <a class="btn small primary" href="#/a/${esc(a.id)}">All checks and provenance</a></figcaption></figure>`;
    $("#modal").hidden = false;
  }
  function howItWorks() {
    const steps = [
      ["phone", "Capture offline", "Each photo is stored and searchable on the officer's device in Qdrant Edge, with no network."],
      ["sync", "Sync what matters", "Urgent reports go first, faces are blurred on the device, repeat shots are linked instead of re-sent."],
      ["shield", "Verify in the cloud", "Cloudinary AI Vision tags and captions each photo; every one is checked for reuse, place, date and edits."],
      ["report", "Report to funders", "Before/after pairs, campaign images and a donor report, each photo traceable to its original."],
    ];
    return `<section class="section how"><h2>How it works</h2><ol class="steps4">${steps.map(([i, t, d], n) =>
      `<li><span class="ic">${icon(i)}</span><span class="n">${n + 1}</span><b>${esc(t)}</b><span>${esc(d)}</span></li>`).join("")}</ol></section>`;
  }
  function overviewMap(projects, assets) {
    const el = $("#omap");
    if (!el) return;
    if (!window.L) { el.innerHTML = `<div class="empty">The map could not load.</div>`; return; }
    if (S.omap) { S.omap.remove(); S.omap = null; }
    const m = L.map(el, { scrollWheelZoom: false });
    S.omap = m;
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "© OpenStreetMap contributors" }).addTo(m);
    const colors = { verified: "#1E7A4C", review: "#C58B1B", flagged: "#A83232" };
    let bounds = null;
    projects.forEach((p) => {
      L.circle([p.lat, p.lon], { radius: p.radius_m, color: "#1E6A50", weight: 1.5, fillOpacity: 0.06 }).addTo(m)
        .bindPopup(`<b>${esc(p.name)}</b><br>${esc(p.evidence)} photos · ${esc(p.verified)} verified<br><a href="#/p/${esc(p.id)}">Open project</a>`);
      const area = L.latLng(p.lat, p.lon).toBounds(p.radius_m * 2);
      bounds = bounds ? bounds.extend(area) : area;
    });
    assets.filter((a) => a.lat != null).forEach((a) => {
      L.circleMarker([a.lat, a.lon], { radius: 7, color: "#fff", weight: 2, fillColor: colors[a.integrity_level] || "#555", fillOpacity: 0.95 })
        .addTo(m).bindPopup(`<a href="#/a/${esc(a.id)}"><img src="${esc(pub(a))}" alt=""></a><b>${esc(a.site_name || "")}</b><br>${esc(LEVEL[a.integrity_level] || "")} · ${esc(day(a.captured_at))}`);
    });
    if (bounds) m.fitBounds(bounds.pad(0.1)); else m.setView([22.5, 88.4], 9);
  }

  async function overview() {
    const slow = setTimeout(() => {
      view.innerHTML = `<div class="waking"><span class="spinner" aria-hidden="true"></span><div><b>Waking up the server…</b><div class="muted">This free demo sleeps when nobody is using it. It takes up to a minute to start, then it is quick.</div></div></div>`;
    }, 2500);
    let d, list;
    try {
      [d, list] = await Promise.all([api("/api/overview"), api("/api/assets?limit=200")]);
    } finally { clearTimeout(slow); }
    const assets = list.items || list.assets || [];
    const t = d.totals;
    const st = d.status;
    const withPairs = d.projects.filter((p) => p.pairs);
    const details = await Promise.all(withPairs.map((p) => api(`/api/projects/${encodeURIComponent(p.id)}`).catch(() => null)));
    const pairs = details.filter(Boolean).flatMap((x) => x.pairs || []).filter((x) => x.before && x.after);
    const pair = pairs.find((x) => x.change_text) || pairs[0];
    const sites = new Set(assets.map((a) => a.site_id).filter(Boolean)).size;
    const reuse = assets.filter((a) => ((a.integrity && a.integrity.checks) || []).some((c) => c.key === "reuse" && c.status === "fail")).length;
    const faces = assets.filter((a) => a.faces && !a.consent).length;
    const tagged = assets.filter((a) => (a.ai_tags || []).length).length;
    const reportTo = (pair && pair.project_id) || (d.projects[0] && d.projects[0].id);
    const kpi = (ic, n, k, sub, cls = "") => `<div class="kpi2 ${cls}"><span class="ic">${icon(ic)}</span><div><div class="v" data-count="${n}">${n}</div><div class="k">${esc(k)}</div>${sub ? `<div class="sub">${esc(sub)}</div>` : ""}</div></div>`;
    view.innerHTML = `
      <section class="hero">
        <div class="eyebrow light">${esc(d.org)}</div>
        <h1>Verified field evidence, from a phone with no signal to a funder's report</h1>
        <p>Field teams capture and search photos offline. The office sees every photo checked by AI for place, date, reuse and edits, paired before and after, and ready for donors.</p>
        <div class="row hero-cta">
          <a class="btn gold" href="#gallery">Explore evidence</a>
          ${pair ? `<a class="btn ghost" href="#beforeafter">See before/after</a>` : ""}
          ${reportTo ? `<a class="btn ghost" href="#/report/${esc(reportTo)}">Donor report</a>` : ""}
        </div>
        <div class="badges"><span>Built on Qdrant Edge</span><span>Cloudinary AI Vision</span><span>Sample photos from Wikimedia Commons</span></div>
      </section>
      <div class="kpis2">
        ${kpi("check", t.verified, "Photos verified", `of ${t.evidence} photos`, "ok")}
        ${kpi("pin", sites, "Sites", "placed from GPS")}
        ${kpi("folder", d.projects.length, "Projects")}
        ${kpi("copy", reuse, "Reuse flagged", "same photo, another project", reuse ? "bad" : "")}
        ${kpi("face", faces, "Faces blurred", "photos with people, in public images")}
        ${kpi("tag", tagged, "AI-tagged", "by Cloudinary AI Vision")}
      </div>
      ${pair ? `<section class="section" id="beforeafter"><div class="section-head"><h2>Before and after</h2><span class="muted">Drag the handle to compare</span></div>
        <div class="feature-pair">${pairCard(pair)}</div></section>` : ""}
      ${howItWorks()}
      <section class="section"><div class="section-head"><h2>Projects and sites</h2><button class="btn" id="newp" data-write>New project</button></div>
        <div class="map-row"><div class="omap" id="omap" role="region" aria-label="Map of project areas and photo locations"></div>
        <div class="projects stack">${d.projects.map(projectCard).join("")}</div></div>
        <div class="legend"><span class="dot verified"></span>Verified <span class="dot review"></span>Needs review <span class="dot flagged"></span>Flagged</div></section>
      <section class="section" id="gallery"><div class="section-head"><h2>Evidence</h2><a href="#/search">Search all evidence</a></div>
        <div class="gallery">${assets.map(galleryCard).join("") || `<div class="empty">No evidence yet.</div>`}</div></section>
      <section class="section cols">
        <div class="panel"><h2>Flagged for review</h2>
          ${d.flagged.length ? `<div class="list">${d.flagged.map((a) => { const p = firstProblem(a); return `<a class="it" href="#/a/${esc(a.id)}"><img src="${esc(pub(a))}" alt=""><div><div class="row">${levelPill(a)}<span class="muted">${esc(a.project_name || "")}</span></div><div>${esc(p ? p.message : "")}</div></div></a>`; }).join("")}</div>`
          : `<p class="muted">Nothing flagged. Reused photos, pictures taken outside a project's area or dates, and edited files appear here.</p>`}
        </div>
        <div class="panel"><h2>Activity</h2>
          <ul class="feed">${d.events.map((e) => `<li class="${esc(e.level)}"><span class="t">${esc(tsTime(e.ts))}</span><span>${esc(e.message)}</span></li>`).join("") || `<li><span></span><span class="muted">No activity yet.</span></li>`}</ul>
          <h3 style="margin-top:18px">System</h3>
          <dl class="status-list">
            <dt>Media store</dt><dd>${st.store === "cloudinary" ? `Cloudinary · ${esc(st.cloud_name)}` : "Local files (dev mode)"}</dd>
            <dt>AI Vision</dt><dd>${st.ai_vision && !st.ai_vision_error ? "On" : "Off here; results kept from earlier runs"}</dd>
            <dt>Vector index</dt><dd>${st.index === "qdrant" ? "Qdrant (shared with devices)" : "Local"}</dd>
            <dt>Search on this server</dt><dd>${esc(st.embedder.mode === "clip" ? "CLIP ViT-B/32 and keywords" : "Keywords, AI tags and captions")}</dd>
          </dl>
        </div>
      </section>`;
    countUp(view);
    bindPairs(view);
    $$(".gcard img", view).forEach((img) => {
      const done = () => img.parentNode.classList.remove("skel");
      if (img.complete) done(); else { img.addEventListener("load", done); img.addEventListener("error", done); }
    });
    const byId = Object.fromEntries(assets.map((a) => [a.id, a]));
    $$("[data-lightbox]", view).forEach((b) => b.addEventListener("click", () => lightbox(byId[b.dataset.lightbox])));
    $$('a[href="#gallery"], a[href="#beforeafter"]', view).forEach((a) => a.addEventListener("click", (e) => {
      e.preventDefault(); $(a.getAttribute("href")).scrollIntoView({ behavior: reduced() ? "auto" : "smooth" });
    }));
    $("#newp").addEventListener("click", projectModal);
    overviewMap(d.projects, assets);
  }

  function projectModal() {
    const today = new Date().toISOString().slice(0, 10);
    const later = new Date(Date.now() + 180 * 864e5).toISOString().slice(0, 10);
    $("#modal-content").innerHTML = `<h2 id="m-title">New project</h2>
      <p class="muted">Photos taken inside the project area and between its dates pass the location and date checks. Sites inside it are created automatically from GPS.</p>
      <form id="pf" class="form">
        <label>Name<input type="text" name="name" required placeholder="e.g. Lake Cleanup, Salt Lake"></label>
        <label>Activity<input type="text" name="activity" placeholder="e.g. Waste clean-up drive"></label>
        <label style="grid-column:1/-1">Description<textarea name="description" rows="2"></textarea></label>
        <label>Centre latitude<input type="number" step="any" name="lat" required></label>
        <label>Centre longitude<input type="number" step="any" name="lon" required></label>
        <label>Radius (km)<input type="number" step="any" name="radius" value="5" min="0.1" required></label>
        <label>Start date<input type="date" name="start" value="${today}" required></label>
        <label>End date<input type="date" name="end" value="${later}" required></label>
      </form>
      <div class="row"><button class="btn" id="geo" type="button">Use my location</button><button class="btn primary" id="psave" type="button">Create project</button></div>`;
    $("#modal").hidden = false;
    const f = $("#pf");
    $("#geo").addEventListener("click", () => {
      if (!navigator.geolocation) { toast("Location is not available in this browser"); return; }
      navigator.geolocation.getCurrentPosition((pos) => { f.lat.value = pos.coords.latitude.toFixed(5); f.lon.value = pos.coords.longitude.toFixed(5); },
        () => toast("Could not read your location; type the coordinates instead"));
    });
    $("#psave").addEventListener("click", async () => {
      if (!f.reportValidity()) return;
      try {
        const p = await post("/api/projects", { name: f.name.value, activity: f.activity.value, description: f.description.value,
          lat: Number(f.lat.value), lon: Number(f.lon.value), radius_m: Number(f.radius.value) * 1000, start_date: f.start.value, end_date: f.end.value });
        $("#modal").hidden = true; toast("Project created. Devices pick it up on their next sync."); location.hash = `#/p/${p.id}`;
      } catch (e) { toast(e.message); }
    });
  }
  function projectCard(p) {
    return `<a class="pcard" href="#/p/${esc(p.id)}">
      <div class="cover" style="${p.cover ? `background-image:url('${esc(p.cover)}')` : ""}">${p.cover ? "" : `<span class="none">No evidence yet</span>`}</div>
      <div class="body"><div class="name">${esc(p.name)}</div><div class="act">${esc(p.activity)}</div>
        <div class="nums"><span><b>${p.evidence}</b> photos</span><span><b style="color:var(--ok)">${p.verified}</b> verified</span>${p.flagged ? `<span><b style="color:var(--bad)">${p.flagged}</b> flagged</span>` : ""}<span><b>${p.pairs}</b> before/after</span></div>
        <div class="muted" style="font-size:12.5px">${esc(day(p.start_date))} – ${esc(day(p.end_date))}</div>
      </div></a>`;
  }

  // ---------------------------------------------------------------- project
  async function project(id, tab = "evidence") {
    const d = await api(`/api/projects/${encodeURIComponent(id)}`);
    const p = d.project;
    const a = d.assets;
    const counts = { verified: 0, review: 0, flagged: 0 };
    a.forEach((x) => { counts[x.integrity_level] = (counts[x.integrity_level] || 0) + 1; });
    view.innerHTML = `
      <div class="crumbs"><a href="#/">Overview</a> / ${esc(p.name)}</div>
      <div class="page-head"><div>
        <div class="eyebrow">${esc(p.activity)}</div><h1>${esc(p.name)}</h1>
        <p>${esc(p.description)}</p>
        <p class="mono">${esc(day(p.start_date))} – ${esc(day(p.end_date))} · area radius ${(p.radius_m / 1000).toFixed(1)} km · centre ${p.lat.toFixed(4)}, ${p.lon.toFixed(4)}</p>
      </div><div class="row"><a class="btn" href="#/upload?project=${esc(p.id)}">Upload</a><a class="btn primary" href="#/report/${esc(p.id)}">Donor report</a></div></div>
      <div class="kpis">
        <div class="kpi"><div class="k">Evidence</div><div class="v">${a.length}</div></div>
        <div class="kpi ok"><div class="k">Verified</div><div class="v">${counts.verified}</div></div>
        <div class="kpi warn"><div class="k">Needs review</div><div class="v">${counts.review}</div></div>
        <div class="kpi bad"><div class="k">Flagged</div><div class="v">${counts.flagged}</div></div>
        <div class="kpi"><div class="k">Sites with evidence</div><div class="v">${d.sites.filter((s) => s.evidence).length}</div></div>
        <div class="kpi"><div class="k">Before / after</div><div class="v">${d.pairs.length}</div></div>
      </div>
      <nav class="tabs">
        ${[["evidence", "Evidence", a.length], ["map", "Map & sites", d.sites.filter((s) => s.evidence).length], ["compare", "Before / after", d.pairs.length], ["activity", "Activity", ""]]
          .map(([k, n, c]) => `<a href="#/p/${esc(p.id)}/${k}" class="${tab === k ? "active" : ""}">${n}<span class="count">${c}</span></a>`).join("")}
      </nav>
      <div id="tab"></div>`;
    const el = $("#tab");
    if (tab === "evidence") evidenceTab(el, p, a);
    else if (tab === "map") mapTab(el, p, a, d.sites);
    else if (tab === "compare") compareTab(el, p, a, d.pairs);
    else el.innerHTML = `<div class="panel"><ul class="feed">${d.events.map((e) => `<li class="${esc(e.level)}"><span class="t">${esc(tsTime(e.ts))}</span><span>${esc(e.message)}</span></li>`).join("") || "<li><span></span><span class='muted'>No activity yet.</span></li>"}</ul></div>`;
  }

  function evidenceTab(el, p, assets) {
    const tags = Array.from(new Set(assets.flatMap((a) => a.ai_tags || []))).sort();
    el.innerHTML = `
      <div class="toolbar">
        <input type="search" id="pq" placeholder="Search this project: “plastic in the water”, “saplings”, “clean street”">
        <div class="seg" id="lv">${[["", "All"], ["verified", "Verified"], ["review", "Needs review"], ["flagged", "Flagged"]].map(([k, n], i) => `<button data-l="${k}" class="${i ? "" : "on"}">${n}</button>`).join("")}</div>
        <select id="tg"><option value="">All AI tags</option>${tags.map((t) => `<option value="${esc(t)}">${esc(cap(t))}</option>`).join("")}</select>
      </div>
      <p class="mono muted" id="pinfo"></p>
      <div class="grid" id="pgrid"></div>`;
    let level = "", tag = "", results = null;
    const render = () => {
      let list = results || assets;
      if (level) list = list.filter((a) => a.integrity_level === level);
      if (tag) list = list.filter((a) => (a.ai_tags || []).includes(tag));
      $("#pgrid").innerHTML = list.length ? list.map(evCard).join("") : `<div class="empty">No evidence matches. Photos synced from field devices and web uploads land here.</div>`;
    };
    $$("#lv button").forEach((b) => b.addEventListener("click", () => { level = b.dataset.l; $$("#lv button").forEach((x) => x.classList.toggle("on", x === b)); render(); }));
    $("#tg").addEventListener("change", (e) => { tag = e.target.value; render(); });
    let timer;
    $("#pq").addEventListener("input", (e) => {
      clearTimeout(timer);
      const q = e.target.value.trim();
      timer = setTimeout(async () => {
        if (!q) { results = null; $("#pinfo").textContent = ""; render(); return; }
        const r = await api(`/api/search?q=${encodeURIComponent(q)}&project_id=${encodeURIComponent(p.id)}`);
        results = r.items;
        $("#pinfo").textContent = `${r.items.length} result(s) · ${r.mode} · ${r.latency_ms} ms`;
        render();
      }, 280);
    });
    render();
  }

  function mapTab(el, p, assets, sites) {
    const used = sites.filter((s) => s.evidence);
    el.innerHTML = `<div class="map-wrap"><div class="map" id="map"></div>
      <div class="sites">${used.map((s) => `<div class="site"><div class="row" style="justify-content:space-between"><span class="n">${esc(s.name)}</span>
        <button class="btn small" data-rename="${esc(s.id)}" data-name="${esc(s.name)}" data-write>Rename</button></div>
        <div class="muted" style="font-size:13px">${s.evidence} photo(s) · ${s.lat.toFixed(4)}, ${s.lon.toFixed(4)}${s.auto ? " · created from GPS" : ""}</div></div>`).join("") || `<div class="empty">No sites with evidence yet.</div>`}</div></div>`;
    $$("[data-rename]", el).forEach((b) => b.addEventListener("click", () => {
      const box = b.closest(".site");
      box.innerHTML = `<form class="row"><input type="text" value="${esc(b.dataset.name)}" aria-label="Site name" style="flex:1"><button class="btn small primary">Save</button></form>`;
      const f = $("form", box); $("input", f).focus();
      f.addEventListener("submit", async (e) => {
        e.preventDefault();
        try { await patch(`/api/sites/${encodeURIComponent(b.dataset.rename)}`, { name: $("input", f).value }); toast("Site renamed"); route(); }
        catch (err) { toast(err.message); }
      });
    }));
    if (!window.L) { $("#map").innerHTML = `<div class="empty">The map library could not load (no internet?). Sites are listed on the right.</div>`; return; }
    if (S.map) { S.map.remove(); S.map = null; }
    const m = L.map("map").setView([p.lat, p.lon], 12);
    S.map = m;
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "© OpenStreetMap contributors" }).addTo(m);
    const area = L.circle([p.lat, p.lon], { radius: p.radius_m, color: "#1E6A50", weight: 1.5, fillOpacity: 0.05 }).addTo(m);
    const colors = { verified: "#1E7A4C", review: "#C58B1B", flagged: "#A83232" };
    const pts = [];
    assets.filter((a) => a.lat != null).forEach((a) => {
      pts.push([a.lat, a.lon]);
      L.circleMarker([a.lat, a.lon], { radius: 8, color: "#fff", weight: 2, fillColor: colors[a.integrity_level] || "#555", fillOpacity: 0.95 })
        .addTo(m).bindPopup(`<a href="#/a/${esc(a.id)}"><img src="${esc(a.thumb_url)}" alt=""></a><b>${esc(a.site_name || "")}</b><br>${esc(LEVEL[a.integrity_level] || "")} · ${esc(day(a.captured_at))}`);
    });
    if (pts.length) m.fitBounds(L.latLngBounds(pts).pad(0.4), { maxZoom: 16 }); else m.fitBounds(area.getBounds());
  }

  function compareTab(el, p, assets, pairs) {
    const opts = assets.map((a) => `<option value="${esc(a.id)}">${esc(day(a.captured_at))} · ${esc(a.site_name || "")} · ${esc((a.note || "").slice(0, 40))}</option>`).join("");
    el.innerHTML = `
      ${pairs.length ? `<div class="pairs">${pairs.map(pairCard).join("")}</div>` : `<div class="empty">Before/after pairs appear automatically once a site has two photos taken at least an hour apart. You can also pick two photos below.</div>`}
      <div class="panel section" data-write><h2>Compare any two photos</h2>
        <div class="form"><label>Before<select id="mb">${opts}</select></label><label>After<select id="ma">${opts}</select></label></div>
        <button class="btn" id="mk">Create comparison</button></div>`;
    bindPairs(el);
    $("#mk").addEventListener("click", async () => {
      try { await post("/api/pairs", { before_id: $("#mb").value, after_id: $("#ma").value }); toast("Comparison created"); route(); }
      catch (e) { toast(e.message); }
    });
  }

  function pairCard(x) {
    if (!x.before || !x.after) return "";
    (S.pairs = S.pairs || {})[x.id] = x;
    const gap = Math.round(Number(x.gap_days) || 0);
    const aiSaysMore = Boolean(x.change_text) && x.verdict === "unclear";  // the tags saw no change; the AI description did
    const composite = x.composite_url ? `<details><summary>Cloudinary composite and how it was made</summary>
        <a href="${esc(x.composite_url)}" target="_blank" rel="noopener"><img src="${esc(x.composite_url)}" alt="Before and after side by side" style="width:100%;border-radius:8px;margin-top:8px"></a>
        <ol class="steps">${x.composite_steps.map((s) => `<li>${esc(s)}</li>`).join("")}</ol></details>` : "";
    return `<article class="pair" data-pair="${x.id}">
      <div class="compare">
        <img src="${esc(x.after.compare_url)}" alt="After" loading="lazy">
        <img class="before" src="${esc(x.before.compare_url)}" alt="Before" loading="lazy">
        <span class="lb l">Before · ${esc(day(x.before.captured_at))}</span><span class="lb r">After · ${esc(day(x.after.captured_at))}</span>
        <div class="handle"></div>
        <input type="range" min="0" max="100" value="50" aria-label="Slide to compare before and after">
      </div>
      <div class="body">
        <div class="row"><strong>${esc(x.site_name)}</strong>${aiSaysMore ? `<span class="pill changed">See AI Vision</span>` : `<span class="pill ${esc(x.verdict)}">${esc(cap(x.verdict))}</span>`}
          <span class="muted mono">${gap} ${gap === 1 ? "day" : "days"} apart${x.similarity != null ? ` · view match ${Math.round(x.similarity * 100)}%` : ""}${x.manual ? " · picked by hand" : ""}</span></div>
        <div>${x.change_text ? "The tag check alone can't tell what changed, so AI Vision compared the two photos:" : esc(x.summary)}</div>
        ${x.change_text ? `<div class="change"><b>AI Vision:</b> ${esc(x.change_text)}</div>` : `<div data-write><button class="btn small" data-describe="${x.id}">Describe the change with AI Vision</button></div>`}
        ${composite}
        <div class="row">${readOnly()
            ? ((x.campaign || []).length ? `<button class="btn small gold" data-campaign-see="${x.id}">See campaign images</button>` : "")
            : `<button class="btn small gold" data-campaign-pair="${x.id}">Make campaign images</button>`}
          <a class="btn small" href="#/a/${esc(x.before.id)}">Before details</a><a class="btn small" href="#/a/${esc(x.after.id)}">After details</a></div>
      </div></article>`;
  }

  function bindPairs(root) {
    $$(".compare", root).forEach((c) => {
      const r = $("input", c), img = $(".before", c), h = $(".handle", c);
      r.addEventListener("input", () => { img.style.clipPath = `inset(0 ${100 - r.value}% 0 0)`; h.style.left = `${r.value}%`; });
    });
    $$("[data-describe]", root).forEach((b) => b.addEventListener("click", async () => {
      b.disabled = true; b.textContent = "Asking AI Vision…";
      try { await post(`/api/pairs/${b.dataset.describe}/describe`); route(); }
      catch (e) { toast(e.message); b.disabled = false; b.textContent = "Describe the change with AI Vision"; }
    }));
    $$("[data-campaign-pair]", root).forEach((b) => b.addEventListener("click", () => campaignModal({ pair_id: Number(b.dataset.campaignPair) }, "")));
    $$("[data-campaign-see]", root).forEach((b) => b.addEventListener("click", () => campaignGallery(S.pairs[b.dataset.campaignSee].campaign)));
  }

  // ---------------------------------------------------------------- asset
  async function asset(id) {
    const d = await api(`/api/assets/${encodeURIComponent(id)}`);
    const a = d.asset, pv = d.provenance, integ = a.integrity || { checks: [] };
    const ringColor = { verified: "var(--ok)", review: "#C58B1B", flagged: "var(--bad)" }[a.integrity_level] || "var(--muted)";
    const icon = { pass: "✓", warn: "!", fail: "✕", info: "i" };
    const ownCampaign = pv.derived.filter((x) => x.purpose === "campaign" && x.asset_id === a.id);
    view.innerHTML = `
      <div class="crumbs"><a href="#/">Overview</a> / <a href="#/p/${esc(a.project_id)}">${esc(a.project_name || "Project")}</a> / Evidence</div>
      <div class="page-head"><div><div class="eyebrow">${esc(a.site_name || "No site")}</div><h1>${esc(a.note || a.caption || "Field photo")}</h1>
        <p>Taken ${esc(dt(a.captured_at))} · ${a.source === "field" ? `synced from ${esc(a.device_name || a.device_id)}` : "uploaded on the web"} · evidence id <span class="mono">${esc(a.id)}</span></p></div>
        <div class="row">${a.media_type === "video" ? "" : readOnly()
          ? (ownCampaign.length ? `<button class="btn gold" id="camp-see">See campaign images</button>` : "")
          : `<button class="btn gold" id="camp">Make campaign images</button>`}</div></div>
      <div class="asset">
        <div>
          <div class="viewer panel" style="padding:12px">
            <div class="row" style="margin-bottom:10px"><div class="seg" id="vw"><button class="on" data-v="staff">Staff view</button><button data-v="public">Public view${a.faces && !a.consent ? " (faces blurred)" : ""}</button></div>
              <a class="btn small" href="${esc(a.secure_url)}" target="_blank" rel="noopener">Original file</a></div>
            ${a.media_type === "video"
              ? `<video id="big" controls preload="metadata" poster="${esc(a.poster_url)}" src="${esc(a.display_url)}" style="width:100%;border-radius:12px;background:#000"></video>`
              : `<img id="big" src="${esc(a.display_url)}" alt="Evidence photo">`}
          </div>
          <div class="panel section"><h2>What the AI sees</h2>
            ${a.caption ? `<p>${esc(a.caption)}</p>` : `<p class="muted">No caption.</p>`}
            <div class="tags">${(a.ai_tags || []).map((t) => `<span class="tag ai">${esc(lbl(t))}</span>`).join("") || `<span class="muted">No tags</span>`}</div>
            <p class="muted mono" style="margin-top:10px">Source: ${esc(a.vision_source || "none")}${(a.device_tags || []).length ? ` · on-device tags: ${esc(a.device_tags.map(lbl).join(", "))}` : ""}</p>
          </div>
          ${d.similar.length ? `<div class="panel section"><h2>Visually similar evidence</h2><div class="similar">${d.similar.map((s) => `<a href="#/a/${esc(s.id)}"><img src="${esc(s.thumb_url)}" alt="">${esc(s.site_name || "")} · ${Math.round(s.similarity * 100)}%</a>`).join("")}</div></div>` : ""}
          ${d.pairs.length ? `<div class="section pairs">${d.pairs.map(pairCard).join("")}</div>` : ""}
        </div>
        <div>
          <div class="panel"><div class="score"><div class="ring" style="--p:${a.integrity_score || 0};--c:${ringColor}"><span>${a.integrity_score ?? "–"}</span></div>
            <div><div class="eyebrow">Integrity</div><h2 style="margin:2px 0">${esc(LEVEL[a.integrity_level] || "Not checked")}</h2><div class="muted" style="font-size:13.5px">Can a funder trust this photo?</div></div></div>
            <ul class="checks">${integ.checks.map((c) => `<li><span class="ic ${esc(c.status)}">${icon[c.status] || "·"}</span><span>${esc(c.message)}${c.key === "reuse" && a.reuse_of ? ` <a href="#/a/${esc(a.reuse_of)}">Open the earlier photo</a>` : ""}</span></li>`).join("")}</ul>
          </div>
          <div class="panel section"><h2>Details</h2><dl class="kv">
            <dt>Project</dt><dd>${esc(a.project_name || "—")}</dd><dt>Site</dt><dd>${esc(a.site_name || "—")}</dd>
            <dt>Location</dt><dd>${a.lat != null ? `${a.lat.toFixed(5)}, ${a.lon.toFixed(5)}` : "No GPS"}</dd>
            <dt>Status reported</dt><dd>${esc(cap(a.status_claim || "—"))}</dd><dt>Camera</dt><dd>${esc(a.camera || "—")}</dd>
            <dt>People</dt><dd>${a.faces ? `${a.faces} face(s) · consent ${a.consent ? "recorded" : "not recorded"}` : "None detected"}</dd>
            <dt>Size</dt><dd>${esc(a.width)}×${esc(a.height)} · ${Math.round((a.bytes || 0) / 1024)} KB</dd></dl>
            <details style="margin-top:12px" data-write><summary>Edit note, status or consent</summary>
              <div class="form" style="grid-template-columns:1fr"><label>Note<textarea id="en" rows="2">${esc(a.note || "")}</textarea></label>
              <label>Status<select id="es"><option value="">—</option>${["planned", "in_progress", "completed", "needs_attention", "damaged"].map((s) => `<option value="${s}" ${a.status_claim === s ? "selected" : ""}>${cap(s)}</option>`).join("")}</select></label>
              <label class="inline"><input type="checkbox" id="ec" ${a.consent ? "checked" : ""}> Consent recorded for people shown</label></div>
              <button class="btn small primary" id="esave">Save</button></details>
          </div>
          <div class="panel section"><h2>Provenance</h2><ol class="timeline">
            <li><div class="t">Captured</div><div class="d">${esc(dt(pv.capture.captured_at))} · ${esc(pv.capture.device || "unknown device")}${pv.capture.camera ? ` · ${esc(pv.capture.camera)}` : ""}${pv.capture.lat != null ? ` · ${pv.capture.lat.toFixed(5)}, ${pv.capture.lon.toFixed(5)}` : ""}</div></li>
            ${pv.capture.source === "field" ? `<li><div class="t">Processed on the device</div><div class="d">Searchable offline in Qdrant Edge${pv.capture.blurred_on_device ? "; faces blurred before upload" : ""}; synced when the network returned</div></li>` : ""}
            <li><div class="t">Stored in Cloudinary</div><div class="d mono">${esc(pv.original.public_id)} · v${esc(pv.original.version)}<br>sha256 ${esc((pv.original.sha256 || "").slice(0, 24))}… · pHash ${esc(pv.original.phash || "")}<br>${esc(pv.original.asset_folder || "")}</div></li>
            <li><div class="t">Analysed</div><div class="d">${esc(pv.analysis.vision_source || "none")}${pv.analysis.quality != null ? ` · focus ${pv.analysis.quality.toFixed(2)}` : ""}${pv.metadata_keys.length ? ` · ${pv.metadata_keys.length} metadata fields kept` : ""}</div></li>
            <li><div class="t">Derived outputs (${pv.derived.length})</div><div class="d">${pv.derived.length ? pv.derived.map((x) => `<details><summary>${esc(x.name)}</summary><ol class="steps">${x.steps.map((s) => `<li>${esc(s)}</li>`).join("")}</ol><a class="mono" href="${esc(x.url)}" target="_blank" rel="noopener">open</a></details>`).join("") : "None yet. Campaign images and comparisons will be listed here with every transformation step."}</div></li>
          </ol></div>
        </div>
      </div>`;
    $$("#vw button").forEach((b) => b.addEventListener("click", () => {
      $$("#vw button").forEach((x) => x.classList.toggle("on", x === b));
      if (a.media_type !== "video") $("#big").src = b.dataset.v === "public" ? a.public_display_url : a.display_url;
    }));
    if ($("#camp")) $("#camp").addEventListener("click", () => campaignModal({ asset_id: a.id }, a.note || a.caption || ""));
    if ($("#camp-see")) $("#camp-see").addEventListener("click", () => campaignGallery(ownCampaign));
    $("#esave").addEventListener("click", async () => {
      try { await patch(`/api/evidence/${encodeURIComponent(a.id)}`, { note: $("#en").value, status_claim: $("#es").value || null, consent: $("#ec").checked }); toast("Saved"); route(); }
      catch (e) { toast(e.message); }
    });
    bindPairs(view);
  }

  // ---------------------------------------------------------------- campaign modal
  function campaignModal(target, caption) {
    $("#modal-content").innerHTML = `<h2 id="m-title">Campaign images</h2>
      <p class="muted">Rendered by Cloudinary from the original: cropped around the subject for each format, colours improved, faces blurred unless consent is recorded, caption and credit added. Nothing is edited by hand, so every image stays traceable.</p>
      <label class="form" style="grid-template-columns:1fr;margin:10px 0"><span style="font-weight:600;color:var(--muted);font-size:13px">Caption</span>
        <textarea id="cap" rows="2" maxlength="140">${esc(caption)}</textarea></label>
      <button class="btn primary" id="gen">Create images</button><div class="variants" id="vars"></div>`;
    $("#modal").hidden = false;
    $("#gen").addEventListener("click", async () => {
      const btn = $("#gen"); btn.disabled = true; btn.textContent = "Creating…";
      try {
        const r = await post("/api/campaign", { ...target, caption: $("#cap").value });
        $("#vars").innerHTML = r.variants.map((v) => `<div class="variant"><div class="ph"><img src="${esc(v.url)}" alt="${esc(v.name)}" loading="lazy"></div>
          <div class="b"><b>${esc(v.name)}</b><div class="row"><a class="btn small primary" href="${esc(v.download)}">Download</a><button class="btn small" data-copy="${esc(v.url)}">Copy link</button></div>
          <details><summary>How it was made</summary><ol class="steps">${v.steps.map((s) => `<li>${esc(s)}</li>`).join("")}</ol></details></div></div>`).join("");
        $$("[data-copy]").forEach((b) => b.addEventListener("click", async () => {
          try { await navigator.clipboard.writeText(b.dataset.copy); toast("Link copied"); } catch (_) { toast(b.dataset.copy); }
        }));
      } catch (e) { toast(e.message); }
      btn.disabled = false; btn.textContent = "Create images";
    });
  }
  $$("[data-close]").forEach((x) => x.addEventListener("click", () => ($("#modal").hidden = true)));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#modal").hidden = true; });

  // ---------------------------------------------------------------- search
  async function search(q) {
    view.innerHTML = `<div class="page-head"><div><div class="eyebrow">Search every project</div><h1>Find evidence by what is in it</h1>
      <p>Describe the scene in your own words. Search combines CLIP image understanding with AI Vision tags, captions and field notes.</p></div></div>
      <form class="toolbar" id="sf"><input type="search" id="sq" value="${esc(q)}" placeholder="e.g. storm debris in a courtyard, mangroves along the river, boats at a ghat"><button class="btn primary">Search</button></form>
      <p class="mono muted" id="si"></p><div class="grid" id="sg"></div>`;
    $("#sf").addEventListener("submit", (e) => { e.preventDefault(); location.hash = `#/search?q=${encodeURIComponent($("#sq").value.trim())}`; });
    if (!q) { $("#sq").focus(); return; }
    const r = await api(`/api/search?q=${encodeURIComponent(q)}`);
    $("#si").textContent = `${r.items.length} result(s) · ${r.mode} · ${r.latency_ms} ms`;
    $("#sg").innerHTML = r.items.length ? r.items.map(evCard).join("") : `<div class="empty">No evidence matches “${esc(q)}”.</div>`;
  }

  // ---------------------------------------------------------------- upload
  async function upload(params) {
    if (readOnly()) {
      view.innerHTML = `<div class="page-head"><div><div class="eyebrow">Web upload</div><h1>Uploads are off in this public demo</h1>
        <p>This public demo is read-only to protect the AI quota. ${demoLinks(S.status.public_demo)}</p>
        <p><a class="btn" href="#/">Browse the evidence</a></p></div></div>`;
      return;
    }
    const { projects, status_claims } = await api("/api/projects");
    const pre = params.get("project") || "";
    view.innerHTML = `<div class="page-head"><div><div class="eyebrow">Web upload</div><h1>Upload evidence</h1>
      <p>For photos that did not come through a field device. Keep the original files: photos forwarded on WhatsApp lose their GPS and time, and the integrity check will say so.</p></div></div>
      <div class="panel">
        <label class="drop" id="drop" for="uf"><input type="file" id="uf" accept="image/*,video/*" multiple><b>Drop photos or videos here, or click to choose</b><span class="muted" id="picked">JPEG, PNG, MP4 or MOV, several at once</span></label>
        <div class="form">
          <label>Project<select id="up"><option value="">Detect from GPS</option>${projects.map((p) => `<option value="${esc(p.id)}" ${p.id === pre ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select></label>
          <label>Site status<select id="us"><option value="">—</option>${status_claims.map((s) => `<option value="${s}">${cap(s)}</option>`).join("")}</select></label>
          <label>Note<input type="text" id="un" placeholder="What does this show?"></label>
          <label class="inline"><input type="checkbox" id="uc"> People shown agreed to be photographed</label>
        </div>
        <button class="btn primary" id="ugo" disabled>Upload and check</button>
        <div class="results" id="ures"></div>
      </div>`;
    let files = [];
    const setFiles = (f) => { files = Array.from(f); $("#picked").textContent = files.length ? `${files.length} file(s) selected` : "JPEG, PNG, MP4 or MOV, several at once"; $("#ugo").disabled = !files.length; };
    $("#uf").addEventListener("change", (e) => setFiles(e.target.files));
    const drop = $("#drop");
    ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
    ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
    drop.addEventListener("drop", (e) => setFiles(e.dataTransfer.files));
    $("#ugo").addEventListener("click", async () => {
      const b = $("#ugo"); b.disabled = true; b.textContent = `Uploading ${files.length}…`;
      const fd = new FormData();
      files.forEach((f) => fd.append("files", f, f.name));
      fd.append("project_id", $("#up").value); fd.append("note", $("#un").value); fd.append("status_claim", $("#us").value); fd.append("consent", $("#uc").checked);
      try {
        const { results } = await api("/api/upload", { method: "POST", body: fd });
        $("#ures").innerHTML = results.map((a) => a.error ? `<div class="result"><span></span><div><b>${esc(a.file_name)}</b><div class="why" style="color:var(--bad)">${esc(a.error)}</div></div><span></span></div>`
          : `<a class="result" href="#/a/${esc(a.id)}"><img src="${esc(a.thumb_url)}" alt=""><div><b>${esc(a.site_name || "No site")}</b> · ${esc(a.project_name || "")}<div class="tags">${(a.ai_tags || []).map((t) => `<span class="tag ai">${esc(lbl(t))}</span>`).join("")}</div><div class="muted" style="font-size:13px">${esc((firstProblem(a) || {}).message || "All checks passed")}</div></div>${levelPill(a)}</a>`).join("");
        setFiles([]);
      } catch (e) { toast(e.message); }
      b.textContent = "Upload and check"; b.disabled = !files.length;
    });
  }

  // ---------------------------------------------------------------- report
  async function report(id) {
    const r = await api(`/api/projects/${encodeURIComponent(id)}/report`);
    const k = r.kpis, p = r.project;
    const maxTag = Math.max(1, ...r.tags.map((t) => t.count));
    view.innerHTML = `<div class="report-actions"><a class="btn" href="#/p/${esc(p.id)}">Back to project</a><button class="btn primary" id="print">Print or save as PDF</button></div>
      <article class="report">
        <header><div class="eyebrow">${esc(r.organisation)} · Impact report</div><h1>${esc(p.name)}</h1>
          <div class="muted">${esc(p.activity)} · ${esc(day(k.first_day))} – ${esc(day(k.last_day))} · generated ${esc(dt(r.generated_at))}</div></header>
        <p class="lead">${esc(r.narrative)}</p>
        <div class="kpis section">
          <div class="kpi"><div class="k">Photos</div><div class="v">${k.evidence}</div></div>
          <div class="kpi ok"><div class="k">Verified</div><div class="v">${k.verified_pct}%</div></div>
          <div class="kpi"><div class="k">Sites</div><div class="v">${k.sites}</div></div>
          <div class="kpi"><div class="k">Before / after</div><div class="v">${k.pairs}</div></div>
        </div>
        ${r.pairs.length ? `<section class="section"><h2>Before and after</h2>${r.pairs.filter((x) => x.before && x.after).map((x) => `<figure>
          ${x.composite_url ? `<img src="${esc(x.composite_url)}" alt="Before and after at ${esc(x.site_name)}">` : `<div class="row"><img src="${esc(x.before.public_thumb_url)}" alt="" style="width:49%"><img src="${esc(x.after.public_thumb_url)}" alt="" style="width:49%"></div>`}
          <figcaption><b>${esc(x.site_name)}</b>, ${esc(day(x.before.captured_at))} → ${esc(day(x.after.captured_at))}. ${esc(x.change_text || x.summary)}</figcaption></figure>`).join("")}</section>` : ""}
        ${r.tags.length ? `<section class="section"><h2>What the photos show</h2><div class="bars">${r.tags.map((t) => `<div class="bar"><span>${esc(t.label)}</span><i style="width:${(100 * t.count) / maxTag}%"></i><span class="mono">${t.count}</span></div>`).join("")}</div></section>` : ""}
        ${r.sites.length ? `<section class="section"><h2>Sites</h2><table><thead><tr><th>Site</th><th>Photos</th><th>Location</th></tr></thead><tbody>${r.sites.map((s) => `<tr><td>${esc(s.name)}</td><td>${s.evidence}</td><td class="mono">${s.lat.toFixed(4)}, ${s.lon.toFixed(4)}</td></tr>`).join("")}</tbody></table></section>` : ""}
        ${r.highlights.length ? `<section class="section"><h2>Verified evidence</h2><div class="similar">${r.highlights.map((a) => `<a href="#/a/${esc(a.id)}"><img src="${esc(a.public_thumb_url)}" alt="">${esc(a.site_name || "")} · ${esc(day(a.captured_at))}</a>`).join("")}</div></section>` : ""}
        ${r.flagged.length ? `<section class="section"><h2>Excluded after review</h2><table><tbody>${r.flagged.map((a) => `<tr><td>${esc(a.site_name || "")}</td><td>${esc((firstProblem(a) || {}).message || "")}</td></tr>`).join("")}</tbody></table></section>` : ""}
        <footer>Every photo in this report links to its original in Cloudinary with its capture metadata, integrity checks and the exact transformations applied. Faces of people who have not given consent are blurred. Evidence ids and fingerprints are available on request.</footer>
      </article>`;
    $("#print").addEventListener("click", () => window.print());
  }

  // ---------------------------------------------------------------- router
  async function route() {
    const h = location.hash || "#/";
    const [path, qs] = h.slice(1).split("?");
    const parts = path.split("/").filter(Boolean);
    const params = new URLSearchParams(qs || "");
    $$(".nav a").forEach((a) => a.classList.toggle("active", (parts[0] || "overview") === (a.dataset.nav === "overview" ? "overview" : a.dataset.nav)));
    try {
      if (!parts.length) await overview();
      else if (parts[0] === "p") await project(parts[1], parts[2] || "evidence");
      else if (parts[0] === "a") await asset(parts[1]);
      else if (parts[0] === "search") await search(params.get("q") || "");
      else if (parts[0] === "upload") await upload(params);
      else if (parts[0] === "report") await report(parts[1]);
      else await overview();
    } catch (e) {
      view.innerHTML = `<div class="empty">Could not load this page: ${esc(e.message)}. <a href="#/">Back to overview</a></div>`;
    }
  }
  window.addEventListener("hashchange", () => { window.scrollTo(0, 0); route(); });
  loadStatus().then(route);
  setInterval(loadStatus, 30000);
})();
