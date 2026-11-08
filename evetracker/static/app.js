// Click a column header to sort. Cells may carry data-v with a raw sortable value.
document.addEventListener("click", (e) => {
  const th = e.target.closest("table.sortable th");
  if (!th) return;
  const table = th.closest("table");
  const idx = [...th.parentNode.children].indexOf(th);
  const dir = th.dataset.dir === "desc" ? "asc" : "desc";
  table.querySelectorAll("th").forEach((h) => delete h.dataset.dir);
  th.dataset.dir = dir;
  const tbody = table.tBodies[0];
  const key = (row) => {
    const cell = row.children[idx];
    if (!cell) return "";
    const v = cell.dataset.v ?? cell.textContent.trim();
    const n = parseFloat(v);
    return isNaN(n) || cell.dataset.v === undefined && !/^[-\d.,]+$/.test(v) ? v.toLowerCase() : n;
  };
  const rows = [...tbody.rows];
  rows.sort((a, b) => {
    const x = key(a), y = key(b);
    const c = typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y));
    return dir === "asc" ? c : -c;
  });
  rows.forEach((r) => tbody.appendChild(r));
});

// Confirmation for destructive forms.
document.addEventListener("submit", (e) => {
  const msg = e.target.dataset.confirm;
  if (msg && !confirm(msg)) e.preventDefault();
});

// Broken item icons are swapped for a placeholder by the inline script in base.html; this also
// catches any that failed before that listener existed.
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("img").forEach((img) => {
    if (img.complete && img.naturalWidth === 0 && window.EVE_PLACEHOLDER && img.src !== window.EVE_PLACEHOLDER) {
      img.src = window.EVE_PLACEHOLDER;
      img.classList.add("noimg");
    }
  });
});

// Copy buttons: <button data-copy="element-id">. navigator.clipboard only works on https or
// localhost, and the app is usually opened at http://<pi-ip>, so fall back to execCommand.
document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-copy]");
  if (!btn) return;
  const text = document.getElementById(btn.dataset.copy).textContent.trim();
  let ok = false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      ok = true;
    }
  } catch (_) {}
  if (!ok) {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { ok = document.execCommand("copy"); } catch (_) {}
    ta.remove();
  }
  const label = btn.textContent;
  btn.textContent = ok ? "Copied ✓" : "Select & copy manually";
  setTimeout(() => (btn.textContent = label), 1800);
});

// ---------- small notification ----------
function toast(message, bad) {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = message;
  el.className = "show" + (bad ? " bad" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (el.className = ""), 4000);
}

// ---------- copy a literal value: <button data-copy-text="1233000.00"> ----------
async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (_) {}
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (_) {}
  ta.remove();
  return ok;
}

document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-copy-text]");
  if (!btn) return;
  e.preventDefault();
  const ok = await copyText(btn.dataset.copyText);
  toast(ok ? `Copied ${btn.dataset.copyText}: paste it into the order in EVE` : "Couldn't copy: select the number instead", !ok);
});

// ---------- "Open in EVE": market window (data-market) and autopilot destination (data-waypoint) ----------
document.addEventListener("click", async (e) => {
  const btn = e.target.closest(".eve-btn");
  if (!btn) return;
  e.preventDefault();      // also stops <summary> from toggling when the button sits inside one
  e.stopPropagation();
  const url = btn.dataset.market ? `/ui/market/${btn.dataset.market}` : `/ui/waypoint/${btn.dataset.waypoint}`;
  btn.disabled = true;
  try {
    const r = await fetch(url, { method: "POST" });
    const data = await r.json().catch(() => ({ ok: false, message: `Error ${r.status}` }));
    toast(data.message, !data.ok);
  } catch (err) {
    toast("Couldn't reach the tracker: " + err, true);
  } finally {
    btn.disabled = false;
  }
}, true);

// ---------- CSV export: a button above every data table, exporting what's shown ----------
function tableToCsv(table) {
  const esc = (v) => /[",\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v;
  const rows = [];
  for (const tr of table.querySelectorAll("tr")) {
    if (tr.closest("table") !== table) continue;
    const cells = [...tr.children].map((c) => {
      // Prefer the raw sortable value (exact numbers instead of "1.23B").
      // textContent for headers: innerText would apply the CSS upper-casing.
      const raw = c.tagName === "TH" ? c.textContent : (c.dataset.v !== undefined ? c.dataset.v : c.innerText);
      return esc(raw.replace(/\s+/g, " ").replace(/[⧉⌖]/g, "").replace(/\bCopy\b/g, "").trim());
    });
    if (cells.length > 1) rows.push(cells.join(","));
  }
  return rows.join("\r\n");
}

function addCsvButtons() {
  document.querySelectorAll("table.sortable").forEach((table, i) => {
    if (table.dataset.csv) return;
    table.dataset.csv = "1";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "small csv-btn";
    btn.textContent = "⬇ CSV";
    btn.title = "Download this table as a spreadsheet (CSV)";
    btn.addEventListener("click", () => {
      const section = table.closest("section, details");
      const heading = section && section.querySelector("h2, summary span");
      const name = [document.title.split("–")[0], heading ? heading.textContent : "", new Date().toISOString().slice(0, 10)]
        .map((s) => s.trim()).filter(Boolean).join(" ").replace(/[^\w ]+/g, " ").trim().replace(/\s+/g, "-").toLowerCase();
      const blob = new Blob(["﻿" + tableToCsv(table)], { type: "text/csv;charset=utf-8" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = (name || "eve-tracker-" + i) + ".csv";
      document.body.appendChild(a);
      a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
    });
    const bar = document.createElement("div");
    bar.className = "table-tools";
    bar.appendChild(btn);
    table.parentNode.insertBefore(bar, table);
  });
}
document.addEventListener("DOMContentLoaded", addCsvButtons);
