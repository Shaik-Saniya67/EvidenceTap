// Shared helpers for EvidenceTap pages.
const ET = (() => {
  const KEY = "evidencetap.records.v1";

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function records() {
    try { return JSON.parse(localStorage.getItem(KEY) || "[]"); } catch { return []; }
  }
  function saveRecord(r) {
    const list = records().filter((x) => x.id !== r.id);
    list.unshift(r);
    try { localStorage.setItem(KEY, JSON.stringify(list.slice(0, 50))); } catch {}
  }
  function removeRecord(id) {
    try { localStorage.setItem(KEY, JSON.stringify(records().filter((x) => x.id !== id))); } catch {}
  }
  function tokenFor(id) {
    const r = records().find((x) => x.id === id);
    return r ? r.token : null;
  }

  async function api(path, { method = "GET", token, json, body, headers = {} } = {}) {
    const h = { ...headers };
    if (token) h["X-Token"] = token;
    let b = body;
    if (json !== undefined) { h["Content-Type"] = "application/json"; b = JSON.stringify(json); }
    const res = await fetch(path, { method, headers: h, body: b });
    if (!res.ok) {
      let msg = res.statusText;
      try { msg = (await res.json()).detail || msg; } catch {}
      const e = new Error(msg); e.status = res.status; throw e;
    }
    const ct = res.headers.get("content-type") || "";
    return ct.includes("application/json") ? res.json() : res;
  }

  function fmtTime(iso) {
    if (!iso) return "-";
    const d = new Date(iso);
    return d.toLocaleString(undefined, { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }
  const short = (h, n = 10) => (h ? h.slice(0, n) + "..." + h.slice(-6) : "");

  function toast(msg, ms = 2600) {
    const t = document.createElement("div");
    t.className = "toast"; t.setAttribute("role", "status"); t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), ms);
  }
  async function copy(text) {
    try { await navigator.clipboard.writeText(text); toast("Copied"); }
    catch { prompt("Copy this link:", text); }
  }

  function header(active) {
    const links = [["/", "Capture", "capture"], ["/#records", "My records", "records"], ["/verify", "Verify", "verify"]];
    document.getElementById("top").innerHTML =
      `<div class="wrap"><a class="brand" href="/"><span class="dot"></span><b>EvidenceTap</b></a>` +
      `<nav aria-label="Main">${links.map(([h, l, k]) => `<a href="${h}" class="${k === active ? "on" : ""}">${l}</a>`).join("")}</nav></div>`;
  }
  function footer() {
    const f = document.getElementById("foot");
    if (!f) return;
    f.innerHTML =
      `<div class="helplines"><a class="btn help small" href="tel:112">Call 112 (emergency)</a>` +
      `<a class="btn help small" href="tel:181">Call 181 (women helpline)</a></div>` +
      `<p class="tiny" style="margin-top:12px">EvidenceTap helps organise evidence and shows whether recorded files have changed. ` +
      `It is not legal advice and does not guarantee a recording will be accepted as evidence. ` +
      `This is a hackathon prototype. If you are in danger, call 112 first.</p>`;
  }

  function mapEmbed(lat, lon) {
    const d = 0.004;
    const bbox = [lon - d, lat - d, lon + d, lat + d].join("%2C");
    return `<iframe class="map" title="Map of the recorded location" loading="lazy" ` +
      `src="https://www.openstreetmap.org/export/embed.html?bbox=${bbox}&layer=mapnik&marker=${lat}%2C${lon}"></iframe>` +
      `<p class="tiny"><a href="https://www.openstreetmap.org/?mlat=${lat}&mlon=${lon}#map=17/${lat}/${lon}" target="_blank" rel="noopener">Open in a larger map</a></p>`;
  }

  function integrityBadge(res) {
    if (!res) return "";
    return res.ok
      ? `<span class="badge ok">Intact: ${res.checked} items checked, none changed</span>`
      : `<span class="badge bad">Changed: ${esc(res.reason || "mismatch")} (item ${esc(res.broken_item)})</span>`;
  }

  return { esc, records, saveRecord, removeRecord, tokenFor, api, fmtTime, short, toast, copy, header, footer, mapEmbed, integrityBadge };
})();
