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

// Hide item icons that fail to load (e.g. image server unreachable) instead of showing broken images.
document.addEventListener("error", (e) => {
  if (e.target.tagName === "IMG") e.target.style.visibility = "hidden";
}, true);

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
