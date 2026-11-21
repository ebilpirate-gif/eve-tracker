// Intel page: local checker (with zKillboard stats filled in progressively) and d-scan parser.
(function () {
  const esc = (t) => String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const isk = (v) => !v ? "0" : v >= 1e12 ? (v / 1e12).toFixed(1) + "T" : v >= 1e9 ? (v / 1e9).toFixed(1) + "B" : v >= 1e6 ? (v / 1e6).toFixed(0) + "M" : Math.round(v).toLocaleString();
  const img = (kind, id, size = 32) => `https://images.evetech.net/${kind}/${id}/${kind === "characters" ? "portrait" : "logo"}?size=${size}`;
  const standingClass = (s) => s >= 5 ? "blue" : s > 0 ? "lightblue" : s <= -5 ? "red" : s < 0 ? "orange" : "";
  const standingLabel = (s) => s > 0 ? `+${s}` : s < 0 ? String(s) : "–";

  function debounceOnPaste(el, fn) {
    let t;
    const go = () => { clearTimeout(t); t = setTimeout(() => fn(el.value), 250); };
    el.addEventListener("input", go);
    el.addEventListener("paste", () => setTimeout(go, 0));
  }

  // ---------- local checker ----------
  const localInput = document.getElementById("local-input");
  if (localInput) {
    let seq = 0;
    debounceOnPaste(localInput, async (text) => {
      if (!text.trim()) { document.getElementById("local-result").hidden = true; return; }
      const mine = ++seq;
      document.getElementById("zkill-status").textContent = "looking up…";
      const r = await fetch("/api/intel/local", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) });
      const data = await r.json();
      if (mine !== seq) return;
      if (data.error) { window.toast && toast(data.error, true); return; }
      renderLocal(data);
      sortThreats();
      fillZkill(data.pilots.filter((p) => !p.zkill).map((p) => p.id), mine);
    });

    function renderLocal(d) {
      document.getElementById("local-result").hidden = false;
      document.getElementById("local-stats").innerHTML = `
        <div class="stat big"><span>Pilots</span><b>${d.count}</b></div>
        <div class="stat"><span>Hostile</span><b class="${d.hostile ? "bad" : ""}">${d.hostile}</b></div>
        <div class="stat"><span>Friendly</span><b class="${d.friendly ? "good" : ""}">${d.friendly}</b></div>
        <div class="stat"><span>Neutral</span><b>${d.count - d.hostile - d.friendly}</b></div>
        ${d.unknown.length ? `<div class="stat"><span>Not found</span><b>${d.unknown.length}</b><div class="muted small">${esc(d.unknown.slice(0, 5).join(", "))}</div></div>` : ""}`;
      document.getElementById("local-groups").innerHTML = d.groups.map((g) => `
        <span class="chip ${standingClass(g.standing)}"><img src="${img(g.kind === "alliance" ? "alliances" : "corporations", g.id)}" alt="" loading="lazy">
        <b>${g.count}×</b> ${g.ticker ? `[${esc(g.ticker)}]` : ""} ${esc(g.name || "?")}</span>`).join("");
      const tbody = document.querySelector("#local-table tbody");
      tbody.innerHTML = d.pilots.map((p) => `
        <tr data-id="${p.id}" data-standing="${p.standing}" class="standing-${standingClass(p.standing)}">
          <td><img class="portrait" src="${img("characters", p.id)}" alt="" loading="lazy"> <b>${esc(p.name)}</b></td>
          <td>${p.corp_ticker ? `[${esc(p.corp_ticker)}] ` : ""}<span class="${p.npc_corp ? "muted" : ""}">${esc(p.corporation || "")}</span></td>
          <td>${p.alliance ? `[${esc(p.alliance_ticker || "")}] ${esc(p.alliance)}` : '<span class="muted">–</span>'}</td>
          <td data-v="${p.standing}" title="${esc(p.standing_reason)}"><span class="tag ${standingClass(p.standing)}">${standingLabel(p.standing)}</span></td>
          ${zkillCells(p.zkill)}
          <td><a class="small" href="https://zkillboard.com/character/${p.id}/" target="_blank" rel="noopener">zKill</a></td>
        </tr>`).join("");
      document.dispatchEvent(new Event("tables-changed"));
    }

    function zkillCells(z) {
      if (!z) return `<td class="r muted" colspan="6">…</td>`;
      const danger = z.kills + z.losses ? z.danger : 0;
      return `<td class="r" data-v="${danger}"><span class="danger" style="--d:${danger}%">${z.kills + z.losses ? danger + "%" : "–"}</span></td>
        <td class="r" data-v="${z.kills}" title="${isk(z.isk_destroyed)} ISK destroyed">${z.kills.toLocaleString()}</td>
        <td class="r" data-v="${z.losses}" title="${isk(z.isk_lost)} ISK lost">${z.losses.toLocaleString()}</td>
        <td class="r ${z.recent_kills ? "bad" : "muted"}" data-v="${z.recent_kills}">${z.recent_kills || "–"}</td>
        <td class="r" data-v="${z.gang}">${z.kills ? z.gang + "%" : "–"}</td>
        <td class="small">${z.ships.map((s) => `<span title="${esc(s.group)} · ${s.kills} kills">${esc(s.name)}</span>`).join(", ")}</td>`;
    }

    // Hostiles first, then the most dangerous pilots (once zKillboard danger ratings are in).
    function sortThreats() {
      const tbody = document.querySelector("#local-table tbody");
      const danger = (tr) => Number((tr.children[4] && tr.children[4].dataset.v) || 0);
      [...tbody.rows].sort((a, b) => (Number(a.dataset.standing) - Number(b.dataset.standing)) || (danger(b) - danger(a)))
        .forEach((tr) => tbody.appendChild(tr));
    }

    async function fillZkill(ids, mine) {
      const status = document.getElementById("zkill-status");
      for (let i = 0; i < ids.length; i += 5) {
        if (mine !== seq) return;
        status.textContent = `zKillboard ${Math.min(i + 5, ids.length)} / ${ids.length}…`;
        try {
          const r = await fetch("/api/intel/zkill?ids=" + ids.slice(i, i + 5).join(","));
          const stats = await r.json();
          for (const [id, z] of Object.entries(stats)) {
            const row = document.querySelector(`#local-table tr[data-id="${id}"]`);
            if (!row) continue;
            row.querySelectorAll("td.r.muted[colspan]").forEach((c) => c.remove());
            const link = row.lastElementChild;
            link.insertAdjacentHTML("beforebegin", zkillCells(z));
          }
        } catch (_) {}
      }
      if (mine === seq) {
        status.textContent = ids.length ? "zKillboard stats loaded" : "";
        sortThreats();
      }
    }
  }

  // ---------- d-scan ----------
  const dscanInput = document.getElementById("dscan-input");
  if (dscanInput) {
    debounceOnPaste(dscanInput, async (text) => {
      if (!text.trim()) { document.getElementById("dscan-result").hidden = true; return; }
      const r = await fetch("/api/intel/dscan", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) });
      const d = await r.json();
      document.getElementById("dscan-result").hidden = false;
      document.getElementById("dscan-stats").innerHTML = `
        <div class="stat big"><span>Ships</span><b>${d.ships}</b></div>
        <div class="stat"><span>Everything</span><b>${d.total}</b></div>
        <div class="stat"><span>On grid (&lt;10,000 km)</span><b class="${d.on_grid ? "bad" : ""}">${d.on_grid}</b></div>`;
      document.querySelector("#dscan-groups tbody").innerHTML = d.groups.map((g) =>
        `<tr><td><b>${esc(g.group)}</b></td><td class="muted">${esc(g.kind)}</td><td class="r" data-v="${g.count}">${g.count}</td></tr>`).join("")
        || '<tr><td colspan="3" class="muted">Nothing recognised. Paste d-scan results.</td></tr>';
      document.querySelector("#dscan-types tbody").innerHTML = d.types.map((t) =>
        `<tr><td><img class="icon" src="https://images.evetech.net/types/${t.type_id}/icon?size=32" alt="" loading="lazy"> ${esc(t.name)}</td>
         <td class="muted">${esc(t.group)}</td><td class="r" data-v="${t.count}">${t.count}</td>
         <td class="r ${t.on_grid ? "bad" : "muted"}" data-v="${t.on_grid}">${t.on_grid || "–"}</td></tr>`).join("");
      document.dispatchEvent(new Event("tables-changed"));
    });
  }
})();
