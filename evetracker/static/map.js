// New Eden map: systems coloured by security, stargates, and where your characters / corp members are.
(function () {
  const canvas = document.getElementById("map");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const pop = document.getElementById("map-pop");
  const layers = { chars: true, members: true, assets: false, structures: true, regions: true, incursions: true };
  let sys = [], byId = new Map(), jumps = [], regions = [], live = null;
  // DOTLAN-style intel (hourly): per-system activity, sovereignty, incursions; plus a plotted route.
  let intel = { stats: new Map(), sov: new Map(), owners: {}, incursions: new Set(), max: {} }, routePath = [];
  let colorMode = "security";
  let view = { scale: 1, x: 0, y: 0 };  // screen = world * scale + offset
  let dpr = window.devicePixelRatio || 1, W = 0, H = 0, needsDraw = true, selected = null;

  // EVE's security colours.
  const SEC = ["#8d3264", "#722020", "#bc1117", "#ce440f", "#dc6d07", "#f3fd82", "#71e554", "#60dba3", "#4ecef8", "#399aeb", "#2c75e1"];
  const secColor = (s) => SEC[Math.max(0, Math.min(10, s <= 0 ? 0 : Math.round(s * 10)))];
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  let colors = {};
  function readColors() {
    colors = { bg: css("--bg"), line: css("--line"), text: css("--text"), muted: css("--muted"), accent: css("--accent"),
               good: css("--good"), bad: css("--bad"), panel: css("--panel") };
  }

  function resize() {
    const r = canvas.getBoundingClientRect();
    dpr = window.devicePixelRatio || 1;
    W = r.width; H = r.height;
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    needsDraw = true;
  }
  function fit() {
    // Fit the systems' actual extent (New Eden isn't square).
    let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
    for (const t of sys) { x0 = Math.min(x0, t[2]); x1 = Math.max(x1, t[2]); y0 = Math.min(y0, t[3]); y1 = Math.max(y1, t[3]); }
    if (!isFinite(x0)) { x0 = y0 = 0; x1 = y1 = 1000; }
    const pad = 24, s = Math.min((W - pad * 2) / (x1 - x0 || 1), (H - pad * 2) / (y1 - y0 || 1));
    view = { scale: s, x: (W - (x1 - x0) * s) / 2 - x0 * s, y: (H - (y1 - y0) * s) / 2 - y0 * s };
    needsDraw = true;
  }
  const toScreen = (x, y) => [x * view.scale + view.x, y * view.scale + view.y];
  const toWorld = (sx, sy) => [(sx - view.x) / view.scale, (sy - view.y) / view.scale];

  function zoomAt(sx, sy, factor) {
    const s = Math.max(0.3, Math.min(400, view.scale * factor));
    const [wx, wy] = toWorld(sx, sy);
    view.scale = s; view.x = sx - wx * s; view.y = sy - wy * s;
    needsDraw = true;
  }
  function centerOn(systemId, scale) {
    const s = byId.get(systemId);
    if (!s) return;
    if (scale) view.scale = scale;
    view.x = W / 2 - s[2] * view.scale; view.y = H / 2 - s[3] * view.scale;
    selected = systemId; needsDraw = true;
    showPop(s);
  }

  // ---------- drawing ----------
  function draw() {
    needsDraw = false;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = colors.bg; ctx.fillRect(0, 0, W, H);
    const z = view.scale;
    // stargates
    ctx.strokeStyle = colors.line; ctx.lineWidth = z > 6 ? 1.2 : 0.6; ctx.globalAlpha = z > 3 ? 0.9 : 0.6;
    ctx.beginPath();
    for (const [a, b] of jumps) {
      const A = byId.get(a), B = byId.get(b);
      const [x1, y1] = toScreen(A[2], A[3]), [x2, y2] = toScreen(B[2], B[3]);
      if ((x1 < 0 && x2 < 0) || (x1 > W && x2 > W) || (y1 < 0 && y2 < 0) || (y1 > H && y2 > H)) continue;
      ctx.moveTo(x1, y1); ctx.lineTo(x2, y2);
    }
    ctx.stroke(); ctx.globalAlpha = 1;
    // systems
    const r = Math.max(1, Math.min(5, z * 0.9));
    for (const s of sys) {
      const [x, y] = toScreen(s[2], s[3]);
      if (x < -5 || y < -5 || x > W + 5 || y > H + 5) continue;
      const [color, size] = systemStyle(s, r);
      ctx.fillStyle = color;
      if (size > r) { ctx.globalAlpha = 0.85; ctx.beginPath(); ctx.arc(x, y, size / 2, 0, Math.PI * 2); ctx.fill(); ctx.globalAlpha = 1; }
      else ctx.fillRect(x - size / 2, y - size / 2, size, size);
    }
    if (layers.incursions && intel.incursions.size) {
      ctx.strokeStyle = "#b45cff"; ctx.lineWidth = 1.5;
      for (const sid of intel.incursions) {
        const s = byId.get(sid); if (!s) continue;
        const [x, y] = toScreen(s[2], s[3]);
        ctx.beginPath(); ctx.arc(x, y, Math.max(4, r + 2), 0, Math.PI * 2); ctx.stroke();
      }
    }
    if (routePath.length > 1) drawRoute();
    // system names when zoomed in
    if (z > 9) {
      ctx.font = "11px system-ui, sans-serif"; ctx.fillStyle = colors.muted; ctx.textAlign = "left";
      for (const s of sys) {
        const [x, y] = toScreen(s[2], s[3]);
        if (x < 0 || y < 0 || x > W || y > H) continue;
        ctx.fillText(`${s[1]} ${s[4].toFixed(1)}`, x + 5, y - 4);
      }
    }
    // region names when zoomed out
    if (layers.regions && z < 9) {
      ctx.font = `${z > 3 ? 13 : 11}px system-ui, sans-serif`; ctx.textAlign = "center";
      ctx.fillStyle = colors.muted; ctx.globalAlpha = 0.85;
      const placed = [];  // skip labels that would overlap one already drawn (zoom in to see them)
      for (const g of regions) {
        const [x, y] = toScreen(g[2], g[3]);
        if (x < -50 || y < -20 || x > W + 50 || y > H + 20) continue;
        const w = ctx.measureText(g[1]).width, box = [x - w / 2 - 3, y - 12, x + w / 2 + 3, y + 4];
        if (placed.some((b) => box[0] < b[2] && box[2] > b[0] && box[1] < b[3] && box[3] > b[1])) continue;
        placed.push(box);
        ctx.fillText(g[1], x, y);
      }
      ctx.globalAlpha = 1;
    }
    if (live) drawLive();
    if (selected && byId.get(selected)) {
      const s = byId.get(selected), [x, y] = toScreen(s[2], s[3]);
      ctx.strokeStyle = colors.text; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.arc(x, y, 9, 0, Math.PI * 2); ctx.stroke();
    }
  }

  function activity(sid, mode) {
    const st = intel.stats.get(sid); if (!st) return 0;
    return mode === "kills" ? st[1] + st[2] : mode === "jumps" ? st[0] : mode === "npc" ? st[3] : 0;
  }
  function hue(id) { let h = 0; const t = String(id); for (let i = 0; i < t.length; i++) h = (h * 31 + t.charCodeAt(i)) % 360; return h; }
  // Distinct colours for the biggest sov holders (similar ids would otherwise get similar hues).
  const SOV_PALETTE = ["#e6194b", "#4363d8", "#ffe119", "#3cb44b", "#f58231", "#911eb4", "#46f0f0", "#f032e6", "#bcf60c",
                       "#fabebe", "#008080", "#e6beff", "#9a6324", "#fffac8", "#800000", "#aaffc3", "#808000", "#ffd8b1"];
  let sovRank = new Map();
  function rankSov() {
    const counts = new Map();
    for (const o of intel.sov.values()) counts.set(o, (counts.get(o) || 0) + 1);
    sovRank = new Map([...counts.entries()].sort((a, b) => b[1] - a[1]).map(([o], i) => [o, i]));
  }
  const sovColor = (o) => { const r = sovRank.get(o); return r !== undefined && r < SOV_PALETTE.length ? SOV_PALETTE[r] : `hsl(${hue(o)} 55% 55%)`; };
  function systemStyle(s, r) {
    if (colorMode === "security") return [secColor(s[4]), r];
    if (colorMode === "sov") {
      const owner = intel.sov.get(s[0]);
      return owner ? [sovColor(owner), r * 1.3] : [colors.line, r];
    }
    const v = activity(s[0], colorMode);
    if (!v) return [colors.line, r * 0.8];
    const max = intel.max[colorMode] || 1, t = Math.log1p(v) / Math.log1p(max);
    const base = colorMode === "kills" ? colors.bad : colorMode === "npc" ? colors.good : colors.accent;
    return [base, r + t * Math.max(6, view.scale * 1.6)];
  }
  function drawRoute() {
    ctx.strokeStyle = colors.accent; ctx.lineWidth = 3; ctx.globalAlpha = 0.9; ctx.beginPath();
    routePath.forEach((hop, i) => {
      const s = byId.get(hop.system_id); if (!s) return;
      const [x, y] = toScreen(s[2], s[3]);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    });
    ctx.stroke(); ctx.globalAlpha = 1;
    for (const hop of routePath) {
      const s = byId.get(hop.system_id); if (!s) continue;
      const [x, y] = toScreen(s[2], s[3]);
      ctx.fillStyle = hop.kills ? colors.bad : secColor(hop.sec);
      ctx.beginPath(); ctx.arc(x, y, hop.kills ? 5 : 3.5, 0, Math.PI * 2); ctx.fill();
    }
  }
  function updateLegend() {
    const el = document.getElementById("legend-body");
    if (colorMode === "security") {
      el.innerHTML = '<span><i class="sec-hi"></i>High-sec</span> <span><i class="sec-lo"></i>Low-sec</span> <span><i class="sec-null"></i>Null-sec</span>';
    } else if (colorMode === "sov") {
      const counts = new Map();
      for (const o of intel.sov.values()) counts.set(o, (counts.get(o) || 0) + 1);
      const top = [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8);
      el.innerHTML = top.map(([o, n]) => `<span><i style="background:${sovColor(o)}"></i>${esc(intel.owners[o] || o)} (${n})</span>`).join(" ");
    } else {
      const label = { kills: "ship & pod kills", jumps: "jumps", npc: "NPC kills" }[colorMode];
      el.innerHTML = `<span class="scale">0 <b style="background:linear-gradient(90deg, ${colors.line}, ${colorMode === "kills" ? colors.bad : colorMode === "npc" ? colors.good : colors.accent})"></b> ${(intel.max[colorMode] || 0).toLocaleString()} ${label} in the last hour</span>`;
    }
  }

  function drawLive() {
    // asset value per system: circle area proportional to value
    if (layers.assets && live.assets.length) {
      const max = Math.max(...live.assets.map((a) => a[1]));
      ctx.fillStyle = colors.accent; ctx.strokeStyle = colors.accent;
      for (const [sid, v] of live.assets) {
        const s = byId.get(sid); if (!s) continue;
        const [x, y] = toScreen(s[2], s[3]);
        const rad = 4 + 22 * Math.sqrt(v / max);
        ctx.globalAlpha = 0.18; ctx.beginPath(); ctx.arc(x, y, rad, 0, Math.PI * 2); ctx.fill();
        ctx.globalAlpha = 0.8; ctx.lineWidth = 1; ctx.stroke();
      }
      ctx.globalAlpha = 1;
    }
    if (layers.structures) {
      for (const st of live.structures) {
        const s = byId.get(st.system_id); if (!s) continue;
        const [x, y] = toScreen(s[2], s[3]);
        ctx.fillStyle = st.danger || st.low ? colors.bad : colors.good;
        ctx.save(); ctx.translate(x, y); ctx.rotate(Math.PI / 4); ctx.fillRect(-4, -4, 8, 8); ctx.restore();
      }
    }
    // people: corp members as small dots, my characters as labelled markers
    const groups = new Map();
    const add = (p, mine) => {
      if (!p.system_id || !byId.get(p.system_id)) return;
      if (!groups.has(p.system_id)) groups.set(p.system_id, { mine: [], members: [] });
      groups.get(p.system_id)[mine ? "mine" : "members"].push(p);
    };
    if (layers.members) live.members.forEach((m) => add(m, false));
    if (layers.chars) live.characters.forEach((c) => add(c, true));
    ctx.font = "600 12px system-ui, sans-serif"; ctx.textAlign = "left";
    for (const [sid, g] of groups) {
      const s = byId.get(sid), [x, y] = toScreen(s[2], s[3]);
      if (g.members.length) {
        const online = g.members.some((m) => m.online);
        ctx.fillStyle = online ? colors.good : colors.muted;
        ctx.beginPath(); ctx.arc(x, y, 3 + Math.min(6, g.members.length), 0, Math.PI * 2);
        ctx.globalAlpha = 0.55; ctx.fill(); ctx.globalAlpha = 1;
      }
      if (g.mine.length) {
        ctx.strokeStyle = colors.accent; ctx.lineWidth = 2;
        ctx.beginPath(); ctx.arc(x, y, 6, 0, Math.PI * 2); ctx.stroke();
        const label = g.mine.map((c) => c.name).join(", ");
        const tw = ctx.measureText(label).width;
        ctx.fillStyle = colors.panel; ctx.globalAlpha = 0.85; ctx.fillRect(x + 9, y - 9, tw + 8, 17); ctx.globalAlpha = 1;
        ctx.fillStyle = colors.accent; ctx.fillText(label, x + 13, y + 4);
      }
    }
  }

  function loop() { if (needsDraw && sys.length) draw(); requestAnimationFrame(loop); }

  // ---------- popup ----------
  const esc = (t) => String(t ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const isk = (v) => v >= 1e9 ? (v / 1e9).toFixed(2) + "B" : v >= 1e6 ? (v / 1e6).toFixed(1) + "M" : Math.round(v).toLocaleString();
  function showPop(s) {
    const region = (regions.find((g) => g[0] === s[5]) || [])[1] || "";
    const here = live ? live.characters.filter((c) => c.system_id === s[0]) : [];
    const mem = live ? live.members.filter((c) => c.system_id === s[0]) : [];
    const assets = live ? (live.assets.find((a) => a[0] === s[0]) || [0, 0])[1] : 0;
    const structs = live ? live.structures.filter((x) => x.system_id === s[0]) : [];
    const st = intel.stats.get(s[0]) || [0, 0, 0, 0];
    const owner = intel.sov.get(s[0]);
    pop.innerHTML = `<b>${esc(s[1])}</b> <span style="color:${secColor(s[4])}">${s[4].toFixed(1)}</span>
      <div class="muted small">${esc(region)}${owner ? " · " + esc(intel.owners[owner] || owner) : ""}${intel.incursions.has(s[0]) ? ' · <span style="color:#b45cff">Incursion</span>' : ""}</div>
      <div class="small">Last hour: <b class="${st[1] + st[2] ? "bad" : ""}">${st[1]} ships, ${st[2]} pods killed</b> · ${st[0]} jumps · ${st[3]} NPCs</div>
      <div class="small"><a href="https://evemaps.dotlan.net/system/${encodeURIComponent(s[1].replace(/ /g, "_"))}" target="_blank" rel="noopener">DOTLAN</a> ·
        <a href="https://zkillboard.com/system/${s[0]}/" target="_blank" rel="noopener">zKillboard</a> ·
        <a href="#" data-route-to="${esc(s[1])}">Route here</a></div>
      ${here.map((c) => `<div>◉ ${esc(c.name)}${c.ship ? " · " + esc(c.ship) : ""}</div>`).join("")}
      ${mem.length ? `<div class="small">${mem.length} corp member${mem.length > 1 ? "s" : ""}: ${mem.slice(0, 6).map((m) => esc(m.name)).join(", ")}${mem.length > 6 ? "…" : ""}</div>` : ""}
      ${structs.map((x) => `<div class="small">◆ ${esc(x.name)} (${esc(x.state)})</div>`).join("")}
      ${assets ? `<div class="small">Assets here: ${isk(assets)}</div>` : ""}
      ${window.EVE_UI ? `<button type="button" class="eve-btn" data-waypoint="${s[0]}">⌖ Set destination</button>` : ""}`;
    pop.hidden = false;
  }

  // ---------- side lists ----------
  function ago(iso) {
    if (!iso) return "";
    const d = (Date.now() - Date.parse(iso)) / 1000;
    return d < 3600 ? `${Math.round(d / 60)}m ago` : d < 172800 ? `${Math.round(d / 3600)}h ago` : `${Math.round(d / 86400)}d ago`;
  }
  function person(p, mine) {
    const dot = p.online ? "online" : p.online === null || p.online === undefined ? "unknown" : "offline";
    const where = p.system ? `${esc(p.system)} <span style="color:${secColor(p.sec || 0)}">${(p.sec ?? 0).toFixed(1)}</span>` : '<span class="muted">unknown</span>';
    const detail = mine
      ? (p.has_scope ? [p.docked ? "Docked: " + esc(p.docked) : (p.system ? "In space" : ""), p.ship ? esc(p.ship) + (p.ship_name ? ` “${esc(p.ship_name)}”` : "") : ""].filter(Boolean).join(" · ")
                     : "Log in again to share location")
      : [p.where ? esc(p.where) : "", p.ship ? esc(p.ship) : ""].filter(Boolean).join(" · ");
    const seen = p.online ? "online" : (p.last_logout ? "logged off " + ago(p.last_logout) : "");
    return `<button type="button" class="person" data-system="${p.system_id || ""}">
      <span class="dot ${dot}"></span>
      <span class="who"><b>${esc(p.name)}</b> <span class="muted small">${seen}</span><br><span class="small">${where}</span>
      <br><span class="muted small">${detail}</span></span></button>`;
  }
  function renderLists() {
    document.getElementById("list-chars").innerHTML = live.characters.map((c) => person(c, true)).join("") || '<p class="muted small">No characters.</p>';
    const card = document.getElementById("members-card");
    card.hidden = !live.members.length;
    document.getElementById("members-count").textContent = live.members.length
      ? `${live.members.filter((m) => m.online).length} online / ${live.members.length}` : "";
    document.getElementById("list-members").innerHTML = live.members.map((m) => person(m, false)).join("");
    document.getElementById("map-updated").textContent = "Updated " + new Date().toLocaleTimeString();
  }

  // ---------- data ----------
  async function loadLive() {
    try {
      const r = await fetch(canvas.dataset.live);
      if (r.ok) { live = await r.json(); renderLists(); needsDraw = true; }
    } catch (_) {}
  }
  async function loadIntel() {
    try {
      const d = await (await fetch(canvas.dataset.intel)).json();
      intel.stats = new Map(d.stats.map((r) => [r[0], r.slice(1)]));
      intel.sov = new Map(d.sov);
      intel.owners = d.owners; intel.incursions = new Set(d.incursions);
      intel.max = { kills: 0, jumps: 0, npc: 0 };
      for (const st of intel.stats.values()) {
        intel.max.kills = Math.max(intel.max.kills, st[1] + st[2]);
        intel.max.jumps = Math.max(intel.max.jumps, st[0]); intel.max.npc = Math.max(intel.max.npc, st[3]);
      }
      rankSov(); updateLegend(); needsDraw = true;
    } catch (_) {}
  }

  // ---------- route planner ----------
  const routeForm = document.getElementById("route-form");
  function fillRouteFrom() {
    const dl = document.getElementById("route-from-list");
    const chars = (live ? live.characters : []).filter((c) => c.system);
    dl.innerHTML = chars.map((c) => `<option value="${esc(c.system)}">${esc(c.name)}</option>`).join("") +
      sys.map((s) => `<option value="${esc(s[1])}">`).join("");
  }
  async function plotRoute() {
    const from = document.getElementById("route-from").value, to = document.getElementById("route-to").value;
    const flag = document.getElementById("route-flag").value;
    const summary = document.getElementById("route-summary");
    summary.textContent = "Plotting…";
    const r = await fetch(`/api/route?origin=${encodeURIComponent(from)}&destination=${encodeURIComponent(to)}&flag=${flag}`);
    const d = await r.json();
    if (d.error) { summary.innerHTML = `<span class="bad">${esc(d.error)}</span>`; return; }
    routePath = d.path;
    const last = d.path[d.path.length - 1];
    summary.innerHTML = `<b>${d.jumps} jumps</b> ${esc(d.from)} → ${esc(d.to)} · lowest sec <span style="color:${secColor(d.lowest_sec)}">${d.lowest_sec.toFixed(1)}</span>
      · <span class="${d.kills ? "bad" : ""}">${d.kills} kills on route in the last hour</span>
      ${window.EVE_UI ? `<button type="button" class="eve-btn" data-waypoint="${last.system_id}">⌖ Set destination</button>` : ""}`;
    document.getElementById("route-hops").innerHTML = d.path.map((h) =>
      `<span class="hop ${h.kills ? "kills" : ""}" data-system="${h.system_id}" title="${esc(h.region)} · ${h.kills} kills, ${h.jumps} jumps last hour"><i style="background:${secColor(h.sec)}"></i>${esc(h.name)}${h.kills ? ` <b class="bad">${h.kills}</b>` : ""}</span>`).join("");
    document.getElementById("route-clear").hidden = false;
    // zoom to show the whole route
    let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
    for (const h of d.path) { const s = byId.get(h.system_id); if (!s) continue; x0 = Math.min(x0, s[2]); x1 = Math.max(x1, s[2]); y0 = Math.min(y0, s[3]); y1 = Math.max(y1, s[3]); }
    if (isFinite(x0)) {
      const pad = 60, sc = Math.min(60, Math.min((W - pad * 2) / Math.max(x1 - x0, 1), (H - pad * 2) / Math.max(y1 - y0, 1)));
      view = { scale: sc, x: W / 2 - (x0 + x1) / 2 * sc, y: H / 2 - (y0 + y1) / 2 * sc };
    }
    needsDraw = true;
  }
  routeForm.addEventListener("submit", (e) => { e.preventDefault(); plotRoute(); });
  document.getElementById("route-clear").addEventListener("click", () => {
    routePath = []; document.getElementById("route-hops").innerHTML = ""; document.getElementById("route-summary").textContent = "";
    document.getElementById("route-clear").hidden = true; needsDraw = true;
  });
  document.addEventListener("click", (e) => {
    const hop = e.target.closest(".hop[data-system]");
    if (hop) centerOn(Number(hop.dataset.system), Math.max(view.scale, 18));
    const rt = e.target.closest("[data-route-to]");
    if (rt) {
      e.preventDefault();
      document.getElementById("route-to").value = rt.dataset.routeTo;
      const from = document.getElementById("route-from");
      if (!from.value && live) { const c = live.characters.find((x) => x.system); if (c) from.value = c.system; }
      if (from.value) plotRoute(); else from.focus();
    }
  });
  document.getElementById("map-color").addEventListener("change", (e) => { colorMode = e.target.value; updateLegend(); needsDraw = true; });

  async function init() {
    readColors(); resize();
    const data = await (await fetch(canvas.dataset.static)).json();
    sys = data.systems; jumps = data.jumps; regions = data.regions;
    byId = new Map(sys.map((s) => [s[0], s]));
    const dl = document.getElementById("map-systems");
    dl.innerHTML = sys.map((s) => `<option value="${esc(s[1])}">`).join("");
    fit(); requestAnimationFrame(loop);
    await Promise.all([loadLive(), loadIntel()]);
    fillRouteFrom();
    setInterval(loadLive, 60000);
    setInterval(loadIntel, 10 * 60000);
  }

  // ---------- interaction ----------
  window.EVE_UI = document.body.dataset.ui === "1";
  const pointers = new Map();
  let dragged = false, pinchDist = 0;
  canvas.addEventListener("pointerdown", (e) => {
    canvas.setPointerCapture(e.pointerId); pointers.set(e.pointerId, { x: e.offsetX, y: e.offsetY }); dragged = false;
    if (pointers.size === 2) { const [a, b] = [...pointers.values()]; pinchDist = Math.hypot(a.x - b.x, a.y - b.y); }
  });
  canvas.addEventListener("pointermove", (e) => {
    const p = pointers.get(e.pointerId); if (!p) return;
    if (pointers.size === 2) {
      p.x = e.offsetX; p.y = e.offsetY;
      const [a, b] = [...pointers.values()], d = Math.hypot(a.x - b.x, a.y - b.y);
      if (pinchDist) zoomAt((a.x + b.x) / 2, (a.y + b.y) / 2, d / pinchDist);
      pinchDist = d; dragged = true; return;
    }
    const dx = e.offsetX - p.x, dy = e.offsetY - p.y;
    if (Math.abs(dx) + Math.abs(dy) > 2) dragged = true;
    view.x += dx; view.y += dy; p.x = e.offsetX; p.y = e.offsetY; needsDraw = true;
  });
  const up = (e) => {
    const wasDrag = dragged; pointers.delete(e.pointerId); pinchDist = 0;
    if (!wasDrag && e.type === "pointerup") pick(e.offsetX, e.offsetY);
  };
  canvas.addEventListener("pointerup", up);
  canvas.addEventListener("pointercancel", up);
  canvas.addEventListener("wheel", (e) => { e.preventDefault(); zoomAt(e.offsetX, e.offsetY, Math.exp(-e.deltaY * 0.0015)); }, { passive: false });
  function pick(sx, sy) {
    let best = null, bestD = 12 * 12;
    for (const s of sys) {
      const [x, y] = toScreen(s[2], s[3]), d = (x - sx) ** 2 + (y - sy) ** 2;
      if (d < bestD) { bestD = d; best = s; }
    }
    if (best) { selected = best[0]; showPop(best); } else { selected = null; pop.hidden = true; }
    needsDraw = true;
  }
  document.addEventListener("click", (e) => {
    const b = e.target.closest(".person");
    if (b && b.dataset.system) centerOn(Number(b.dataset.system), Math.max(view.scale, 14));
  });
  document.querySelectorAll("[data-layer]").forEach((cb) => {
    layers[cb.dataset.layer] = cb.checked;
    cb.addEventListener("change", () => { layers[cb.dataset.layer] = cb.checked; needsDraw = true; });
  });
  document.getElementById("map-fit").addEventListener("click", () => { selected = null; pop.hidden = true; fit(); });
  document.getElementById("map-search").addEventListener("change", (e) => {
    const name = e.target.value.trim().toLowerCase();
    const s = sys.find((x) => x[1].toLowerCase() === name);
    if (s) centerOn(s[0], Math.max(view.scale, 18));
  });
  window.addEventListener("resize", () => { const c = toWorld(W / 2, H / 2); resize(); view.x = W / 2 - c[0] * view.scale; view.y = H / 2 - c[1] * view.scale; });
  document.addEventListener("themechange", () => { readColors(); updateLegend(); needsDraw = true; });

  init();
})();
