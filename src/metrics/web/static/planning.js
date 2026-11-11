// Course page of the planning prototype: Klasse/Alter switch, alternative dates, municipality
// rows, availability marks in the team table and a hint when a value is not in the suggestions.

const form = document.getElementById("course-form");

// Klasse/Alter: show the inputs of the chosen kind only; at a school only Schulklasse/Lehrkräfte.
const classAge = form.querySelector(".class-age");
const schoolPlaces = JSON.parse(classAge?.dataset.schoolPlaces || "{}");

function updateKind() {
  if (!classAge) return;
  const atSchool = schoolPlaces[form.elements.location?.value.trim()] === true;
  for (const option of classAge.querySelectorAll("[data-kind-option]")) {
    const allowed = !atSchool || option.dataset.atSchool === "1";
    option.hidden = !allowed;
    const radio = option.querySelector("input");
    if (!allowed && radio.checked) radio.checked = false;
  }
  classAge.querySelector("[data-school-hint]").hidden = !atSchool;
  const chosen = form.querySelector('input[name="class_kind"]:checked')?.value;
  for (const block of classAge.querySelectorAll("[data-kind]")) block.hidden = block.dataset.kind !== chosen;
}
form.addEventListener("change", (event) => {
  if (event.target.name === "class_kind" || event.target.name === "location") updateKind();
});
form.elements.location?.addEventListener("input", updateKind);
updateKind();

// Remove buttons for alternative dates and municipality rows.
form.addEventListener("click", (event) => {
  if (event.target.matches("[data-remove]")) {
    event.target.closest(".alt-row, tr").remove();
  }
});

const addAlternative = form.querySelector("[data-add-alternative]");
addAlternative?.addEventListener("click", () => {
  const row = document.createElement("span");
  row.className = "alt-row";
  row.innerHTML = '<input type="date" name="alt_date"> <button type="button" class="link" data-remove>entfernen</button>';
  addAlternative.before(row);
  row.querySelector("input").focus();
});

const addMunicipality = form.querySelector("[data-add-municipality]");
addMunicipality?.addEventListener("click", () => {
  const row = document.createElement("tr");
  row.innerHTML =
    '<td><input name="mun_name" list="dl-municipality"></td>' +
    '<td><input type="number" min="0" name="mun_female" value="0"></td>' +
    '<td><input type="number" min="0" name="mun_male" value="0"></td>' +
    '<td><button type="button" class="link" data-remove>entfernen</button></td>';
  form.querySelector("[data-mun-rows]").append(row);
  row.querySelector("input").focus();
});

// Availability: mark everyone who is already booked at the chosen time, in both team pickers.
const leadSelect = form.querySelector("[data-lead]");
const support = form.querySelector("[data-support]");
let availabilityRequest = 0;
let busy = {};

function updateTeam() {
  for (const option of leadSelect?.options || []) {
    if (!option.value) continue;
    const what = busy[option.value];
    option.textContent = what ? `${option.dataset.name} – belegt: ${what}` : option.dataset.name;
  }
  const lead = leadSelect?.value;
  const leadNote = form.querySelector("[data-lead-note]");
  if (leadNote) leadNote.textContent = lead && busy[lead] ? `Achtung, belegt: ${busy[lead]}` : "";

  const chosen = [];
  const clashes = [];
  for (const label of support?.querySelectorAll("[data-person]") || []) {
    const box = label.querySelector("input");
    const what = busy[label.dataset.person];
    if (box.value === lead) box.checked = false; // leading and supporting at once makes no sense
    label.classList.toggle("is-busy", Boolean(what));
    label.querySelector("[data-busy]").textContent = what ? `belegt: ${what}` : "";
    if (box.checked) {
      chosen.push(box.dataset.name);
      if (what) clashes.push(box.dataset.name);
    }
  }
  if (support) {
    support.querySelector("[data-support-summary]").textContent = chosen.join(", ") || "– niemand –";
    form.querySelector("[data-support-note]").textContent = clashes.length ? `Achtung, belegt: ${clashes.join(", ")}` : "";
  }
}

async function checkAvailability() {
  const date = form.elements.date?.value;
  const id = ++availabilityRequest;
  let found = {};
  if (date) {
    const params = new URLSearchParams({
      date,
      start: form.elements.start_time?.value || "",
      end: form.elements.end_time?.value || "",
      exclude: form.dataset.course || "",
    });
    const response = await fetch(`/api/planung/belegt?${params}`);
    if (id !== availabilityRequest || !response.ok) return;
    found = await response.json();
  }
  busy = found;
  updateTeam();
}
for (const name of ["date", "start_time", "end_time"]) form.elements[name]?.addEventListener("change", checkAvailability);
form.addEventListener("change", (event) => {
  if (event.target.name === "lead" || event.target.name === "support") updateTeam();
});
document.addEventListener("click", (event) => {
  if (support?.open && !support.contains(event.target)) support.open = false; // close like a dropdown
});
checkAvailability();

// New values: say so when a typed value is not among the suggestions (fewer spelling variants).
function hintNewValue(input) {
  const list = document.getElementById(input.getAttribute("list"));
  if (!list) return;
  let note = input.parentElement.querySelector(".new-value");
  const value = input.value.trim();
  const known = [...list.options].some((o) => o.value === value);
  if (!value || known) {
    note?.remove();
    return;
  }
  if (!note) {
    note = document.createElement("span");
    note.className = "new-value";
    input.after(note);
  }
  const folded = value.toLocaleLowerCase("de");
  const similar = [...list.options].map((o) => o.value).find((v) => {
    const other = v.toLocaleLowerCase("de");
    return other !== folded && (other.includes(folded) || folded.includes(other));
  });
  note.textContent = similar ? `Neuer Wert. Gemeint: „${similar}“?` : "Neuer Wert, bitte Schreibweise prüfen.";
}
for (const input of form.querySelectorAll("input[list]")) {
  input.addEventListener("change", () => hintNewValue(input));
  if (input.value) hintNewValue(input);
}
