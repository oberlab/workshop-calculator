// Sessions-over-time chart (overview and analysis page): keeps the form, the URL and the chart in sync.
// The form's data-api attribute names the endpoint; the analysis endpoint needs the team login.
// data-fixed holds filters a page always sends (a project's contexts) without showing them in the URL.

const FORMAT_LOCALE = { decimal: ",", thousands: ".", grouping: [3], currency: ["", " €"] };
const TIME_FORMAT_LOCALE = {
  dateTime: "%A, %e. %B %Y, %X",
  date: "%d.%m.%Y",
  time: "%H:%M:%S",
  periods: ["", ""],
  days: ["Sonntag", "Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag"],
  shortDays: ["So", "Mo", "Di", "Mi", "Do", "Fr", "Sa"],
  months: ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
           "November", "Dezember"],
  shortMonths: ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"],
};
const EMBED_OPTIONS = {
  renderer: "svg",
  formatLocale: FORMAT_LOCALE,
  timeFormatLocale: TIME_FORMAT_LOCALE,
  actions: { export: true, source: false, compiled: false, editor: false },
  downloadFileName: "einsaetze",
  scaleFactor: { png: 3 },
  i18n: { PNG_ACTION: "Als PNG speichern", SVG_ACTION: "Als SVG speichern" },
  // CSP-safe: expressions are interpreted instead of compiled with new Function (no 'unsafe-eval').
  ast: true,
  expr: vega.expressionInterpreter,
  // No injected <style> elements (blocked by the CSP): vendor/vega-embed.css holds the same rules.
  defaultStyle: false,
  tooltip: { disableDefaultStyle: true },
};

const form = document.getElementById("filters");
const chart = document.getElementById("chart");
const empty = document.getElementById("empty");
const meta = document.getElementById("meta");
let requestId = 0;

// Charts sized to their container cannot be drawn while their tab is hidden (zero width):
// keep the latest spec and draw it when the tab is shown.
const pending = new Map();

async function embed(target, spec) {
  if (target.closest("[hidden]")) {
    pending.set(target, spec);
    return;
  }
  pending.delete(target);
  await vegaEmbed(target, spec, EMBED_OPTIONS);
}

function drawPending(view) {
  for (const [target, spec] of pending) if (view.contains(target)) embed(target, spec);
}

function formatDate(iso) {
  const [y, m, d] = iso.split("-");
  return `${d}.${m}.${y}`;
}

function currentParams() {
  const params = new URLSearchParams();
  for (const [name, value] of new FormData(form)) {
    if (value === "") continue;
    if ((name === "from" || name === "to") && form.period.value !== "custom") continue;
    if (name === "bin" && value === "month") continue;
    if (name === "period" && value === "all") continue;
    params.append(name, value);
  }
  return params;
}

function updateForm() {
  const custom = form.period.value === "custom";
  for (const el of form.querySelectorAll(".custom-range")) el.hidden = !custom;
  for (const details of form.querySelectorAll(".checklist")) {
    const checked = details.querySelectorAll("input:checked").length;
    details.querySelector(".selection").textContent = checked ? `(${checked} ausgewählt)` : "(alle)";
  }
}

async function render() {
  updateForm();
  const params = currentParams();
  let query = params.toString();
  history.replaceState(null, "", (query ? `?${query}` : location.pathname) + location.hash);
  const fixed = form.dataset.fixed;
  if (fixed) query = [query, fixed].filter(Boolean).join("&");

  const id = ++requestId;
  renderRanking(query);
  renderFigures(query, id);
  const response = await fetch(`${form.dataset.api}?${query}`);
  if (id !== requestId) return; // a newer request is on its way
  if (response.status === 401) {
    location.assign("/login"); // session expired
    return;
  }
  if (!response.ok) {
    meta.textContent = `Fehler beim Laden der Daten (${response.status}).`;
    return;
  }
  const { spec, meta: info } = await response.json();

  empty.hidden = info.count > 0;
  chart.hidden = info.count === 0;
  if (info.count > 0) await embed(chart, spec);

  for (const [id, value] of [
    ["total-sessions", info.totals.sessions],
    ["total-participants", info.totals.participants],
    ["total-locations", info.totals.locations],
  ]) {
    document.getElementById(id).textContent = value.toLocaleString("de-DE");
  }

  const parts = [];
  if (info.first) parts.push(`Erster Einsatz am ${formatDate(info.first)}, letzter am ${formatDate(info.last)}`);
  if (info.data_as_of) parts.push(`Datenstand: ${info.data_as_of}`);
  meta.textContent = parts.join(" · ");
}

// Further figures on the analysis page: each <section data-figure="<api>"> holds a .chart, an
// optional .empty note and optional [data-meta="<key>"] elements filled from the response's meta.
async function renderFigure(section, query, id) {
  const target = section.querySelector(".chart");
  const note = section.querySelector(".empty");
  const response = await fetch(`${section.dataset.figure}?${query}`);
  if (id !== requestId || !response.ok) return;
  const { spec, meta } = await response.json();
  const hasData = meta.count === undefined || meta.count > 0;
  target.hidden = !hasData;
  if (note) note.hidden = hasData;
  if (hasData) await embed(target, spec);
  for (const el of section.querySelectorAll("[data-meta]")) {
    el.textContent = Number(meta[el.dataset.meta] ?? 0).toLocaleString("de-DE");
  }
}

function renderFigures(query, id) {
  for (const section of document.querySelectorAll("section[data-figure]")) renderFigure(section, query, id);
}

const rankingList = document.getElementById("ranking-list");

function rankingItem(row, max) {
  const li = document.createElement("li");
  if (row.key === null) li.className = "without";
  let avatar;
  if (row.image_url) {
    avatar = document.createElement("img");
    avatar.src = row.image_url;
    avatar.alt = "";
  } else {
    avatar = document.createElement("span");
    avatar.textContent = row.initials;
  }
  avatar.classList.add("avatar");
  const label = document.createElement("div");
  const name = document.createElement("div");
  name.className = "name";
  name.textContent = row.name;
  const bar = document.createElement("div");
  bar.className = "bar";
  const fill = document.createElement("div");
  fill.className = "fill";
  fill.style.width = `${(100 * row.count) / max}%`;
  bar.append(fill);
  label.append(name, bar);
  const count = document.createElement("span");
  count.className = "count";
  count.textContent = row.count.toLocaleString("de-DE");
  li.append(avatar, label, count);
  return li;
}

async function renderRanking(query) {
  if (!rankingList) return;
  const response = await fetch(`/api/instructor-ranking?${query}`);
  if (!response.ok) return;
  const { rows } = await response.json();
  const max = Math.max(1, ...rows.map((row) => row.count));
  rankingList.replaceChildren(...rows.map((row) => rankingItem(row, max)));
}

// Tabs on the analysis page; the open tab is kept in the address (#schularten) across reloads.
const tabs = [...document.querySelectorAll('[role="tab"]')];

function selectTab(tab, focus = false) {
  for (const other of tabs) {
    const selected = other === tab;
    other.setAttribute("aria-selected", selected);
    other.tabIndex = selected ? 0 : -1;
    document.getElementById(other.getAttribute("aria-controls")).hidden = !selected;
  }
  if (focus) tab.focus();
  const view = document.getElementById(tab.getAttribute("aria-controls"));
  history.replaceState(null, "", `${location.search || location.pathname}#${tab.id.slice(4)}`);
  drawPending(view);
}

for (const tab of tabs) {
  tab.addEventListener("click", () => selectTab(tab));
  tab.addEventListener("keydown", (event) => {
    const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
    if (!step) return;
    event.preventDefault();
    selectTab(tabs[(tabs.indexOf(tab) + step + tabs.length) % tabs.length], true);
  });
}
const initialTab = tabs.find((tab) => location.hash && tab.id === `tab-${location.hash.slice(1)}`);
if (initialTab) selectTab(initialTab);

form.addEventListener("change", render);
form.addEventListener("submit", (event) => { event.preventDefault(); render(); });
document.fonts.ready.then(render);
