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
  forceActionsMenu: true, // the "…" menu; without defaultStyle vega-embed would list the actions openly
  tooltip: { disableDefaultStyle: true },
};

const form = document.getElementById("filters");
// Pages with their own figure export (WM-152) leave out vega-embed's menu, so there is one way to save.
if (document.querySelector("[data-export-menu]")) EMBED_OPTIONS.actions = false;
const chart = document.getElementById("chart");
const empty = document.getElementById("empty");
const meta = document.getElementById("meta");
let requestId = 0;
let lastMeta = null; // meta of the latest sessions-over-time response: the period of the selection

// Charts sized to their container cannot be drawn while their tab is hidden (zero width):
// keep the latest spec and draw it when the tab is shown.
const pending = new Map();
// The latest spec of every chart, for the figure export.
const drawn = new Map();

// Vega-Lite turns date strings in a temporal scale domain into a datetime() expression, which the
// CSP-safe interpreter evaluates to nothing (bars of zero width). Timestamps need no expression.
function timestampDomains(spec) {
  for (const channel of Object.values(spec.encoding || {})) {
    const domain = channel?.type === "temporal" && channel.scale?.domain;
    if (Array.isArray(domain)) channel.scale.domain = domain.map((d) => (typeof d === "string" ? Date.parse(d) : d));
  }
  for (const part of spec.layer || []) timestampDomains(part);
  return spec;
}

async function embed(target, spec) {
  spec = timestampDomains(spec);
  drawn.set(target, spec);
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
    const selection = details.querySelector(".selection");
    selection.textContent = checked ? `(${checked} ausgewählt)` : `(${selection.dataset.none || "alle"})`;
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
  renderKpis(query, id);
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
  lastMeta = info;

  empty.hidden = info.count > 0;
  chart.hidden = info.count === 0;
  if (info.count > 0) await embed(chart, spec);

  for (const [id, value] of [
    ["total-sessions", info.totals.sessions],
    ["total-participants", info.totals.participants],
    ["total-locations", info.totals.locations],
  ]) {
    const target = document.getElementById(id); // only on the overview; the analysis page has KPI tiles
    if (target) target.textContent = value.toLocaleString("de-DE");
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

// KPI tiles (analysis and project pages): each [data-kpi="<key>"] tile gets its value and the
// comparison with the period before from the section's data-api endpoint.
const kpis = document.getElementById("kpis");

function kpiNumber(value) {
  return Math.round(value).toLocaleString("de-DE"); // hours are shown as whole hours
}

function kpiChange(figure) {
  if (figure.previous === null) return "";
  const sign = figure.change > 0 ? "+" : figure.change < 0 ? "−" : "±";
  let text = `vorher ${kpiNumber(figure.previous)} · ${sign}${kpiNumber(Math.abs(figure.change))}`;
  if (figure.previous > 0) {
    const percent = Math.round((100 * Math.abs(figure.change)) / figure.previous);
    text += ` (${sign}${percent.toLocaleString("de-DE")} %)`;
  }
  return text;
}

async function renderKpis(query, id) {
  if (!kpis) return;
  const response = await fetch(`${kpis.dataset.api}?${query}`);
  if (id !== requestId || !response.ok) return;
  const { figures, previous, running_until: runningUntil } = await response.json();
  for (const tile of kpis.querySelectorAll("[data-kpi]")) {
    const figure = figures[tile.dataset.kpi];
    tile.querySelector(".kpi-value").textContent = kpiNumber(figure.value);
    tile.querySelector(".kpi-change").textContent = kpiChange(figure);
  }
  let text = "Kein Vergleich: Der gesamte Zeitraum oder ein offener eigener Zeitraum hat keinen Zeitraum davor.";
  if (previous) {
    text = `Vergleich mit dem Zeitraum davor: ${previous.label}.`;
    if (runningUntil) text += ` Der gewählte Zeitraum läuft noch bis ${formatDate(runningUntil)}, der Vergleich ist vollständig.`;
  }
  kpis.querySelector("[data-kpi-period]").textContent = text;
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
  count.className = "ranking-count";
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

// Figure export (WM-152): a chart as PNG or SVG in a fixed size, with the view's title and the period
// and filters of the selection as caption. Drawn here with Vega's headless renderer from the spec the
// page already has (no new request, chart endpoints unchanged) and saved through a blob: URL, which the
// CSP does not restrict (no inline script, no eval: the expression interpreter as for the page).
const EXPORT_SCALE = 2; // PNG in double resolution
const EXPORT_PAD = 20;
const TITLE_SIZE = 22;
const CAPTION_SIZE = 14;
const CAPTION_LINE = 20;

function schoolYear(iso) {
  const year = Number(iso.slice(0, 4));
  return `${year}/${String(year + 1).slice(2)}`;
}

function periodCaption() {
  const preset = form.period.value;
  const from = lastMeta?.period_from;
  const to = lastMeta?.period_to;
  const dates = (a, b) => `${formatDate(a)}–${formatDate(b)}`;
  if (from && to) {
    if (preset === "this_school_year" || preset === "last_school_year") return `Schuljahr ${schoolYear(from)} (${dates(from, to)})`;
    if (preset === "this_year" || preset === "last_year") return from.slice(0, 4);
    return dates(from, to);
  }
  if (from) return `ab ${formatDate(from)}`;
  if (to) return `bis ${formatDate(to)}`;
  return lastMeta?.first ? `gesamter Zeitraum (${dates(lastMeta.first, lastMeta.last)})` : "gesamter Zeitraum";
}

function exportCaption() {
  const parts = [`Zeitraum: ${periodCaption()}`];
  const chosen = new FormData(form);
  const fixed = new URLSearchParams(form.dataset.fixed || "");
  for (const [name, label] of [["category", "Kurskategorie"], ["target_group", "Zielgruppe"], ["context", "Kontext"]]) {
    const values = [...chosen.getAll(name), ...fixed.getAll(name)];
    if (values.length) parts.push(`${label}: ${values.join(", ")}`);
  }
  parts.push(`Stand: ${new Date().toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit", year: "numeric" })}`);
  return parts.join(" · ");
}

function wrapLines(context, text, width) {
  const lines = [];
  let line = "";
  for (const word of text.split(" ")) {
    const next = line ? `${line} ${word}` : word;
    if (line && context.measureText(next).width > width) {
      lines.push(line);
      line = word;
    } else line = next;
  }
  if (line) lines.push(line);
  return lines;
}

function slug(text) {
  const plain = text.toLowerCase().replace(/ä/g, "ae").replace(/ö/g, "oe").replace(/ü/g, "ue").replace(/ß/g, "ss");
  return plain.replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

function escapeXml(text) {
  return String(text).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
}

function saveFile(blob, name) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

// The header (title and caption lines) above the chart, in the chart's own fonts and colours.
function exportLayout(spec, caption, width) {
  const config = spec.config || {};
  const fonts = { title: config.legend?.titleFont || config.font || "sans-serif", body: config.font || "sans-serif" };
  const measure = document.createElement("canvas").getContext("2d");
  measure.font = `${CAPTION_SIZE}px ${fonts.body}`;
  const lines = wrapLines(measure, caption, width - 2 * EXPORT_PAD);
  return {
    fonts,
    lines,
    ink: config.axis?.titleColor || "#333333",
    background: config.background || "#ffffff",
    titleY: EXPORT_PAD + TITLE_SIZE,
    captionY: EXPORT_PAD + TITLE_SIZE + 8 + CAPTION_SIZE,
    header: EXPORT_PAD + TITLE_SIZE + 8 + lines.length * CAPTION_LINE + 12,
  };
}

// The chart fitted into the box below the header, keeping its proportions.
function fit(chartWidth, chartHeight, width, height) {
  const scale = Math.min(width / chartWidth, height / chartHeight, 1);
  const w = chartWidth * scale;
  const h = chartHeight * scale;
  return { x: (width - w) / 2, w, h };
}

async function exportPng(view, layout, title, width, height, box) {
  const chart = await view.toCanvas(EXPORT_SCALE);
  const canvas = document.createElement("canvas");
  canvas.width = width * EXPORT_SCALE;
  canvas.height = height * EXPORT_SCALE;
  const context = canvas.getContext("2d");
  context.scale(EXPORT_SCALE, EXPORT_SCALE);
  context.fillStyle = layout.background;
  context.fillRect(0, 0, width, height);
  context.fillStyle = layout.ink;
  context.font = `bold ${TITLE_SIZE}px ${layout.fonts.title}`;
  context.fillText(title, EXPORT_PAD, layout.titleY, width - 2 * EXPORT_PAD);
  context.font = `${CAPTION_SIZE}px ${layout.fonts.body}`;
  layout.lines.forEach((line, i) => context.fillText(line, EXPORT_PAD, layout.captionY + i * CAPTION_LINE));
  const place = fit(chart.width / EXPORT_SCALE, chart.height / EXPORT_SCALE, width, box);
  context.drawImage(chart, place.x, layout.header, place.w, place.h);
  return new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
}

async function exportSvg(view, layout, title, width, height, box) {
  const chart = new DOMParser().parseFromString(await view.toSVG(), "image/svg+xml").documentElement;
  const place = fit(Number(chart.getAttribute("width")), Number(chart.getAttribute("height")), width, box);
  for (const [key, value] of Object.entries({ x: place.x, y: layout.header, width: place.w, height: place.h })) {
    chart.setAttribute(key, value);
  }
  const text = (y, size, weight, font, content) =>
    `<text x="${EXPORT_PAD}" y="${y}" font-family="${escapeXml(font)}" font-size="${size}" font-weight="${weight}" fill="${escapeXml(layout.ink)}">${escapeXml(content)}</text>`;
  const svg = [
    `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">`,
    `<rect width="${width}" height="${height}" fill="${escapeXml(layout.background)}"/>`,
    text(layout.titleY, TITLE_SIZE, "bold", layout.fonts.title, title),
    ...layout.lines.map((line, i) => text(layout.captionY + i * CAPTION_LINE, CAPTION_SIZE, "normal", layout.fonts.body, line)),
    new XMLSerializer().serializeToString(chart),
    "</svg>",
  ].join("\n");
  return new Blob([svg], { type: "image/svg+xml" });
}

async function exportFigure(button) {
  const section = button.closest(".tab-panel, section");
  const target = section.querySelector(".chart");
  const spec = drawn.get(target);
  if (!spec || target.hidden) return;
  const kind = button.dataset.export;
  const width = Number(button.dataset.width);
  const height = Number(button.dataset.height);
  const viewTitle = section.querySelector(".view-title").textContent.trim();
  const title = form.dataset.title ? `${form.dataset.title}: ${viewTitle}` : viewTitle;
  const layout = exportLayout(spec, exportCaption(), width);
  const box = height - layout.header;

  // The page's spec at the export size; the chart endpoints stay as they are.
  const sized = structuredClone(spec);
  Object.assign(sized, { width, height: box, autosize: { type: "fit", contains: "padding" } });
  vega.formatLocale(FORMAT_LOCALE);
  vega.timeFormatLocale(TIME_FORMAT_LOCALE);
  const runtime = vega.parse(vegaLite.compile(sized).spec, null, { ast: true });
  const view = new vega.View(runtime, { renderer: "none", expr: vega.expressionInterpreter });
  try {
    await view.runAsync();
    const period = periodCaption().replace(/ \(.*\)$/, ""); // "Schuljahr 2025/26" without its dates
    const name = `${slug(title)}_${slug(period)}_${width}x${height}.${kind}`;
    const exporter = kind === "svg" ? exportSvg : exportPng;
    saveFile(await exporter(view, layout, title, width, height, box), name);
  } finally {
    view.finalize();
  }
}

for (const menu of document.querySelectorAll("[data-export-menu]")) {
  const target = menu.closest(".tab-panel, section").querySelector(".chart");
  const buttons = menu.querySelectorAll("[data-export]");
  // Only a chart that is drawn (data for the selection) can be saved.
  menu.addEventListener("toggle", () => {
    for (const button of buttons) button.disabled = !drawn.has(target) || target.hidden;
  });
  for (const button of buttons) {
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await exportFigure(button);
      } finally {
        button.disabled = false;
      }
    });
  }
}
