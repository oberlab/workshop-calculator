// Sessions-over-time page: keeps the filter form, the URL and the chart in sync.

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
  const response = await fetch(`/api/sessions-over-time?${query}`);
  if (id !== requestId) return; // a newer request is on its way
  if (!response.ok) {
    meta.textContent = `Fehler beim Laden der Daten (${response.status}).`;
    return;
  }
  const { spec, meta: info } = await response.json();

  empty.hidden = info.count > 0;
  chart.hidden = info.count === 0;
  if (info.count > 0) await vegaEmbed(chart, spec, EMBED_OPTIONS);

  const parts = [`${info.count.toLocaleString("de-DE")} Einsätze in der Auswahl`];
  if (info.first) parts.push(`erster am ${formatDate(info.first)}, letzter am ${formatDate(info.last)}`);
  if (info.data_as_of) parts.push(`Datenstand: ${info.data_as_of}`);
  meta.textContent = parts.join(" · ");
}

form.addEventListener("change", render);
form.addEventListener("submit", (event) => { event.preventDefault(); render(); });
document.fonts.ready.then(render);
