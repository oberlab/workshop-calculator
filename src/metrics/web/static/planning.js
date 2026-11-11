// Course page of the planning prototype: Klasse/Alter switch, alternative dates, municipality
// rows, availability marks in the team table and a hint when a value is not in the suggestions.

const form = document.getElementById("course-form");

// Klasse/Alter: show the inputs of the chosen kind only.
function updateKind() {
  const chosen = form.querySelector('input[name="class_kind"]:checked')?.value;
  for (const block of form.querySelectorAll("[data-kind]")) block.hidden = block.dataset.kind !== chosen;
}
form.addEventListener("change", (event) => {
  if (event.target.name === "class_kind") updateKind();
});
updateKind();

// Remove buttons for alternative dates and municipality rows.
form.addEventListener("click", (event) => {
  if (event.target.matches("[data-remove]")) {
    event.target.closest(".alt-row, tr").remove();
    checkAvailability();
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

// Availability: mark everyone who is already booked at the chosen time.
let availabilityRequest = 0;
async function checkAvailability() {
  const date = form.elements.date?.value;
  const rows = form.querySelectorAll("tr[data-person]");
  const id = ++availabilityRequest;
  let busy = {};
  if (date) {
    const params = new URLSearchParams({
      date,
      start: form.elements.start_time?.value || "",
      end: form.elements.end_time?.value || "",
      exclude: form.dataset.course || "",
    });
    const response = await fetch(`/api/planung/belegt?${params}`);
    if (id !== availabilityRequest || !response.ok) return;
    busy = await response.json();
  }
  for (const row of rows) {
    const what = busy[row.dataset.person];
    const picked = row.querySelector("input:checked") !== null;
    row.classList.toggle("is-busy", Boolean(what));
    row.classList.toggle("is-clash", Boolean(what) && picked);
    row.querySelector("[data-busy]").textContent = what ? `belegt: ${what}` : "";
  }
}
for (const name of ["date", "start_time", "end_time"]) form.elements[name]?.addEventListener("change", checkAvailability);
form.addEventListener("change", (event) => {
  if (event.target.name === "lead" || event.target.name === "support") checkAvailability();
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
