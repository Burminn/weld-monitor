"use strict";
const $ = (s, r = document) => r.querySelector(s);
const NS = "http://www.w3.org/2000/svg";

function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat()) if (c != null) e.append(c.nodeType ? c : document.createTextNode(c));
  return e;
}
function svg(tag, attrs, ...kids) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
  for (const c of kids.flat()) if (c != null) e.append(c.nodeType ? c : document.createTextNode(c));
  return e;
}
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(path + " → " + r.status);
  return r.json();
}

const STATE = {
  work: ["Praca", "work"], idle: ["Postój", "idle"], alarm: ["Alarm", "alarm"],
  offline: ["Brak połączenia", "offline"], unknown: ["Brak danych", "offline"],
};
const COLORS = { work: "var(--work)", idle: "var(--idle)", alarm: "var(--alarm)", offline: "var(--off)" };

const fmtTime = ts => new Date(ts * 1000).toLocaleTimeString("pl-PL", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const fmtDT = ts => new Date(ts * 1000).toLocaleString("pl-PL", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
function fmtDur(s) {
  s = Math.max(0, Math.round(s));
  if (s < 60) return s + " s";
  const m = Math.floor(s / 60);
  if (m < 60) return m + " min";
  return Math.floor(m / 60) + " h " + String(m % 60).padStart(2, "0") + " min";
}
const fmtMin = s => (s / 60).toLocaleString("pl-PL", { maximumFractionDigits: 0 });
const pct = x => x == null ? "—" : (x * 100).toLocaleString("pl-PL", { maximumFractionDigits: 1 }) + "%";

// ---------- zakładki
const views = ["przeglad", "produkcja", "zdarzenia", "polaczenie"];
let current = "przeglad";
function show() {
  const v = location.hash.slice(1);
  current = views.includes(v) ? v : "przeglad";
  for (const n of views) $("#v-" + n).hidden = n !== current;
  document.querySelectorAll("#tabs a").forEach(a => a.classList.toggle("on", a.dataset.view === current));
  if (current === "produkcja") loadReport();
  if (current === "zdarzenia") { loadEvents(); loadEventMeta(); }
  if (current === "polaczenie") loadConn();
}
addEventListener("hashchange", show);

// ---------- przegląd
const cardEls = {};

function buildCard(r) {
  const el = {};
  el.root = h("article", { class: "card" });
  el.title = h("h3", {}, r.name);
  el.sub = h("div", { class: "sub" }, `Sterownik ${r.controller} · robot nr ${r.robot_no} · ${r.ip}`);
  el.pill = h("span", { class: "pill" });
  el.since = h("span", { class: "muted" });
  el.prog = h("div", { class: "prog" });
  el.alarm = h("div", { class: "alarmbox", hidden: true });
  const kv = (k, key) => { el[key] = h("div", { class: "v" }); return h("div", { class: "kv" }, h("div", { class: "k" }, k), el[key]); };
  el.grid = h("div", { class: "grid4" }, kv("Sztuki – zmiana", "pShift"), kv("Sztuki – doba", "pDay"), kv("Śr. cykl (zmiana)", "cyc"), kv("Licznik robota", "cnt"));
  el.spark = svg("svg", { viewBox: "0 0 240 56", preserveAspectRatio: "none", role: "img", "aria-label": "Prąd spawania, ostatnie odczyty" });
  el.liveVals = h("div", { class: "live-vals" });
  el.live = h("div", { class: "live-row" }, el.spark, el.liveVals);
  el.root.append(el.title, el.sub, h("div", { class: "state" }, el.pill, el.since), el.prog, el.alarm, el.grid, el.live);
  return el;
}

function drawSpark(svgEl, trace) {
  svgEl.replaceChildren();
  const pts = trace.filter(p => p[1] != null);
  if (pts.length < 2) return;
  const max = Math.max(250, ...pts.map(p => p[1]));
  const path = pts.map((p, i) => `${(i / (pts.length - 1) * 240).toFixed(1)},${(54 - p[1] / max * 50).toFixed(1)}`).join(" ");
  svgEl.append(svg("line", { x1: 0, x2: 240, y1: 54, y2: 54, stroke: "var(--line)", "stroke-width": 1 }),
    svg("polyline", { points: path, fill: "none", stroke: "var(--accent)", "stroke-width": 1.8, "vector-effect": "non-scaling-stroke", "stroke-linejoin": "round" }));
}

function updateCard(el, r, now) {
  const [label, cls] = STATE[r.state] || STATE.unknown;
  el.root.className = "card s-" + cls;
  el.pill.className = "pill " + cls;
  el.pill.textContent = label;
  el.since.textContent = r.since ? "od " + fmtDur(now - r.since) : "";
  el.prog.textContent = r.program || (r.connected ? "—" : (r.error || ""));
  el.alarm.hidden = !r.alarm;
  el.alarm.textContent = r.alarm ? "⚠ " + r.alarm : "";
  el.pShift.textContent = r.pieces_shift;
  el.pDay.textContent = r.pieces_day;
  el.cyc.replaceChildren(r.avg_cycle_shift ? Math.round(r.avg_cycle_shift) : "—", h("small", {}, r.avg_cycle_shift ? " s" : ""));
  el.cnt.textContent = r.counter ?? "—";
  const hasLive = r.trace.some(p => p[1] != null);
  el.live.hidden = !hasLive;
  if (hasLive) {
    drawSpark(el.spark, r.trace);
    el.liveVals.replaceChildren(h("div", {}, "Prąd ", h("b", {}, r.current_a ?? "—"), " A"), h("div", {}, "Napięcie ", h("b", {}, r.voltage_v ?? "—"), " V"));
  }
}

async function tick() {
  try {
    const s = await api("/api/state");
    $("#link").className = "link ok";
    $("#shiftInfo").textContent = s.shift ? `Zmiana ${s.shift.name} (do ${fmtTime(s.shift.end).slice(0, 5)})` : "";
    $("#clock").textContent = fmtTime(s.now);
    for (const r of s.robots) {
      if (!cardEls[r.id]) { cardEls[r.id] = buildCard(r); $("#cards").append(cardEls[r.id].root); }
      updateCard(cardEls[r.id], r, s.now);
    }
  } catch (e) { $("#link").className = "link bad"; }
}

async function loadRecent() {
  try {
    const ev = await api("/api/events?limit=8");
    $("#recentEvents").replaceChildren(eventTable(ev, false));
  } catch (e) {}
}

// ---------- zdarzenia
const robotNames = {};
function eventTable(rows, withHead = true) {
  const lv = { err: "Błąd", warn: "Uwaga", info: "Info" };
  const t = h("table", {});
  if (withHead) t.append(h("thead", {}, h("tr", {}, h("th", {}, "Czas"), h("th", {}, "Stanowisko"), h("th", {}, "Poziom"), h("th", {}, "Zdarzenie"))));
  const tb = h("tbody", {});
  const fmt = withHead ? fmtFull : fmtDT;
  for (const e of rows) tb.append(h("tr", {}, h("td", {}, fmt(e.ts)), h("td", {}, e.robot ? (robotNames[e.robot] || e.robot) : "system"),
    h("td", {}, h("span", { class: "lvl " + e.level }, lv[e.level] || e.level)), h("td", {}, e.text)));
  if (!rows.length) tb.append(h("tr", {}, h("td", { colspan: 4, class: "muted" }, "Brak zdarzeń")));
  t.append(tb);
  return t;
}
const fmtFull = ts => new Date(ts * 1000).toLocaleString("pl-PL", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
const EV_PAGE = 100;
let evApplied = null, evOffset = 0;
function evFilterFromForm() {
  return { robot: $("#evRobot").value, from: $("#evFrom").value, to: $("#evTo").value,
    level: $("#evLevel").value, kind: $("#evKind").value, q: $("#evQ").value.trim() };
}
function evQuery(f, extra = {}) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries({ ...f, ...extra })) if (v !== "" && v != null) p.set(k, v);
  return p.toString();
}
function updateExports(f) {
  const base = { robot: f.robot, from: f.from, to: f.to };
  $("#exEvents").href = "/api/export/events.csv?" + evQuery(f);
  $("#exStates").href = "/api/export/state_log.csv?" + evQuery(base);
  $("#exPieces").href = "/api/export/pieces.csv?" + evQuery(base);
}
async function loadEvents(reset = false) {
  if (reset === true || !evApplied) { evApplied = evFilterFromForm(); evOffset = 0; }
  const f = evApplied;
  try {
    const r = await fetch("/api/events?" + evQuery(f, { limit: EV_PAGE, offset: evOffset }));
    if (!r.ok) throw new Error(await r.text());
    const total = parseInt(r.headers.get("X-Total-Count") || "0", 10);
    const rows = await r.json();
    $("#evTable").replaceWith(Object.assign(eventTable(rows), { id: "evTable" }));
    const a = total ? evOffset + 1 : 0, b = evOffset + rows.length;
    $("#evCount").textContent = total ? `${a}–${b} z ${total}` : "Brak zdarzeń dla wybranych filtrów";
    $("#evPrev").disabled = evOffset === 0;
    $("#evNext").disabled = b >= total;
    updateExports(f);
  } catch (e) { $("#evCount").textContent = "Nie udało się pobrać zdarzeń"; }
}
async function loadEventMeta() {
  try {
    const m = await api("/api/events/meta");
    const sel = $("#evKind"), cur = sel.value;
    const names = { alarm: "alarm", conn: "łączność", program: "program", counter: "licznik", monitor: "monitor" };
    sel.replaceChildren(h("option", { value: "" }, "wszystkie"), ...m.kinds.map(k => h("option", { value: k }, names[k] || k)));
    sel.value = cur;
    $("#evSpan").textContent = m.n ? `W bazie: ${m.n} zdarzeń, od ${fmtFull(m.first)} do ${fmtFull(m.last)}. Dane są przechowywane bez ograniczenia czasu; historia zaczyna się od pierwszego uruchomienia monitora.` : "";
  } catch (e) {}
}

// ---------- produkcja
const iso = d => d.toLocaleDateString("sv-SE"); // RRRR-MM-DD w czasie lokalnym
function initRange() {
  const to = new Date(), from = new Date(Date.now() - 2 * 864e5);
  $("#rTo").value = iso(to); $("#rFrom").value = iso(from);
}
function repQuery() {
  return `from=${$("#rFrom").value}&to=${$("#rTo").value}&group=${$("#rGroup").value}`;
}
async function loadReport() {
  const q = repQuery();
  $("#csvLink").href = "/api/report.csv?" + q;
  let d;
  try { d = await api("/api/report?" + q); } catch (e) { $("#repSummary").replaceChildren(h("div", { class: "tile" }, "Nie można pobrać raportu (sprawdź daty).")); return; }
  const rows = d.rows.filter(r => !r.future);
  // kafelki: suma sztuk na stanowisko
  const tot = {};
  for (const r of rows) { tot[r.robot] ??= { name: r.robot_name, n: 0, work: 0, all: 0 }; tot[r.robot].n += r.pieces; tot[r.robot].work += r.work_s; tot[r.robot].all += r.work_s + r.idle_s + r.alarm_s + r.offline_s; }
  $("#repSummary").replaceChildren(...Object.values(tot).map(t => h("div", { class: "tile" },
    h("div", { class: "k" }, t.name + " · sztuki w zakresie"), h("div", { class: "v" }, t.n.toLocaleString("pl-PL")),
    h("div", { class: "k" }, "wykorzystanie " + pct(t.all ? t.work / t.all : null)))));
  drawChart(rows);
  // tabela
  const t = h("table", {}, h("thead", {}, h("tr", {}, ...["Okres", "Stanowisko"].map(x => h("th", {}, x)),
    ...["Sztuki", "Śr. cykl [s]", "Praca [min]", "Postój [min]", "Alarm [min]", "Brak danych [min]"].map(x => h("th", { class: "n" }, x)),
    h("th", {}, "Rozkład czasu"), h("th", { class: "n" }, "Wykorzystanie"))));
  const tb = h("tbody", {});
  for (const r of rows) {
    const total = r.work_s + r.idle_s + r.alarm_s + r.offline_s || 1;
    const seg = (k, c, label) => h("i", { style: `width:${(r[k] / total * 100).toFixed(2)}%;background:${c}`, title: `${label}: ${fmtDur(r[k])}` });
    tb.append(h("tr", {}, h("td", {}, r.label), h("td", {}, r.robot_name), h("td", { class: "n" }, r.pieces),
      h("td", { class: "n" }, r.avg_cycle_s ?? "—"), h("td", { class: "n" }, fmtMin(r.work_s)), h("td", { class: "n" }, fmtMin(r.idle_s)),
      h("td", { class: "n" }, fmtMin(r.alarm_s)), h("td", { class: "n" }, fmtMin(r.offline_s)),
      h("td", {}, h("div", { class: "stack" }, seg("work_s", COLORS.work, "Praca"), seg("idle_s", COLORS.idle, "Postój"), seg("alarm_s", COLORS.alarm, "Alarm"), seg("offline_s", COLORS.offline, "Brak danych"))),
      h("td", { class: "n" }, pct(r.util))));
  }
  t.append(tb);
  $("#repTable").replaceWith(Object.assign(t, { id: "repTable" }));
}

function drawChart(rows) {
  const labels = [...new Set(rows.map(r => r.label))];
  const robots = [...new Set(rows.map(r => r.robot))];
  const names = Object.fromEntries(rows.map(r => [r.robot, r.robot_name]));
  const val = (l, rb) => (rows.find(r => r.label === l && r.robot === rb) || {}).pieces || 0;
  const max = Math.max(10, ...rows.map(r => r.pieces));
  const W = 960, H = 240, L = 40, B = 46, T = 14, pw = W - L - 10, ph = H - B - T;
  const slot = pw / Math.max(1, labels.length), bw = Math.min(40, slot * 0.8 / robots.length);
  const palette = ["var(--accent)", "var(--s2)", "var(--work)", "var(--off)"];
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Sztuki w okresach", style: "width:100%;height:auto" });
  for (let i = 0; i <= 4; i++) {
    const y = T + ph - ph * i / 4;
    s.append(svg("line", { x1: L, x2: W - 10, y1: y, y2: y, stroke: "var(--line)" }),
      svg("text", { x: L - 6, y: y + 4, "text-anchor": "end", "font-size": 11, fill: "var(--muted)" }, Math.round(max * i / 4)));
  }
  labels.forEach((l, i) => {
    const x0 = L + slot * i + (slot - bw * robots.length) / 2;
    robots.forEach((rb, j) => {
      const v = val(l, rb), bh = ph * v / max, x = x0 + j * bw;
      s.append(svg("rect", { x, y: T + ph - bh, width: bw - 2, height: Math.max(bh, 0), rx: 3, fill: palette[j % 4] }, svg("title", {}, `${names[rb]} · ${l}: ${v} szt.`)));
      if (labels.length <= 12) s.append(svg("text", { x: x + (bw - 2) / 2, y: T + ph - bh - 4, "text-anchor": "middle", "font-size": 11, fill: "var(--text)" }, v));
    });
    if (labels.length <= 14 || i % Math.ceil(labels.length / 14) === 0) {
      const tx = L + slot * i + slot / 2;
      s.append(svg("text", { x: tx, y: H - 24, "text-anchor": "middle", "font-size": 11, fill: "var(--muted)" }, l.split(" ")[0].slice(5)),
        l.includes("zm.") ? svg("text", { x: tx, y: H - 10, "text-anchor": "middle", "font-size": 11, fill: "var(--muted)" }, l.split(" ").slice(1).join(" ")) : null);
    }
  });
  const legend = h("div", { class: "legend" }, robots.map((rb, j) => h("span", { style: `--c:${palette[j % 4]}` }, names[rb])));
  $("#repChart").replaceChildren(s, legend);
}

// ---------- połączenie
async function loadConn() {
  const c = await api("/api/config");
  $("#connIntro").replaceChildren(h("strong", {}, "Uruchomienie komunikacji OPC UA z robotami G4"),
    h("ul", {},
      h("li", {}, "Procedura wg instrukcji „Podłączenie robota serii G4 do sieci LAN” v1.1: checklista dla każdego robota poniżej, z numerami stron instrukcji."),
      h("li", {}, "Adresy zmiennych odczytywanych z robota wpisuje się w pliku nodes.json; aktualne mapowanie jest w tabeli na dole strony."),
      h("li", {}, "Port i politykę zabezpieczeń (Security Policy) ustawia się w config.json; muszą być takie same jak w robocie.")));
  const box = $("#connRobots");
  box.replaceChildren();
  for (const r of c.robots) {
    robotNames[r.id] = r.name;
    const st = c.checklist_state[r.id] || {};
    const done = c.checklist.filter(x => st[x.id]).length;
    const bar = h("i", { style: `width:${done / c.checklist.length * 100}%` });
    const cnt = h("span", { class: "muted" }, `${done} / ${c.checklist.length} kroków`);
    const list = h("div", {}, c.checklist.map(x => h("label", { class: "check" },
      h("input", { type: "checkbox", checked: !!st[x.id], onchange: async ev => {
        await api("/api/checklist/" + r.id, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ step: x.id, done: ev.target.checked }) });
        st[x.id] = ev.target.checked;
        const n = c.checklist.filter(y => st[y.id]).length;
        bar.style.width = n / c.checklist.length * 100 + "%"; cnt.textContent = `${n} / ${c.checklist.length} kroków`;
      } }), h("span", {}, x.text), h("span", { class: "pg" }, x.page))));
    box.append(h("div", { class: "panel rob" }, h("h3", {}, r.name),
      h("div", { class: "kvlist" },
        h("span", {}, "Sterownik: ", h("b", {}, r.controller)), h("span", {}, "Nr robota: ", h("b", {}, r.robot_no)),
        h("span", {}, "Adres: ", h("b", { class: "mono" }, `${r.ip}:${r.port}`)), h("span", {}, "Login: ", h("b", {}, r.username || "—"))),
      cnt, h("div", { class: "prog-bar" }, bar), list));
  }
  const t = h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Metryka"), h("th", {}, "Opis"), h("th", {}, "NodeId"))));
  const tb = h("tbody", {});
  for (const [k, m] of Object.entries(c.nodes)) tb.append(h("tr", {}, h("td", { class: "mono" }, k), h("td", {}, m.opis), h("td", { class: "mono" }, m.node_id || "— nie zmapowano —")));
  t.append(tb);
  $("#nodesTable").replaceWith(Object.assign(t, { id: "nodesTable" }));
  fillRobotSelect(c.robots);
}
function fillRobotSelect(robots) {
  const sel = $("#evRobot");
  if (sel.options.length > 1) return;
  for (const r of robots) { robotNames[r.id] = r.name; sel.append(h("option", { value: r.id }, r.name)); }
}

// ---------- start
$("#repForm").addEventListener("submit", e => { e.preventDefault(); loadReport(); });
$("#evForm").addEventListener("submit", e => { e.preventDefault(); loadEvents(true); loadEventMeta(); });
$("#evReset").addEventListener("click", () => { $("#evForm").reset(); loadEvents(true); });
$("#evPrev").addEventListener("click", () => { evOffset = Math.max(0, evOffset - EV_PAGE); loadEvents(); });
$("#evNext").addEventListener("click", () => { evOffset += EV_PAGE; loadEvents(); });
["#evRobot", "#evLevel", "#evKind"].forEach(s => $(s).addEventListener("change", () => loadEvents(true)));
initRange();
api("/api/config").then(c => fillRobotSelect(c.robots)).catch(() => {});
show();
tick(); loadRecent();
setInterval(tick, 1000);
setInterval(() => { if (current === "przeglad") loadRecent(); if (current === "zdarzenia" && evOffset === 0) loadEvents(); }, 5000);
