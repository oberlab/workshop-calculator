// Sessions-over-time chart (overview and analysis page): keeps the form, the URL and the chart in sync.
// The form's data-api attribute names the endpoint; the analysis endpoint needs the team login.

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
};

const form = document.getElementById("filters");
const chart = document.getElementById("chart");
const empty = document.getElementById("empty");
const meta = document.getElementById("meta");
let requestId = 0;

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
  const query = params.toString();
  history.replaceState(null, "", query ? `?${query}` : location.pathname);

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
  if (info.count > 0) await vegaEmbed(chart, spec, EMBED_OPTIONS);

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
  if (hasData) await vegaEmbed(target, spec, EMBED_OPTIONS);
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

form.addEventListener("change", render);
form.addEventListener("submit", (event) => { event.preventDefault(); render(); });
document.fonts.ready.then(render);
