// Course page (WM-110): Klasse/Alter switch, alternative dates, municipality rows and availability
// marks in the team pickers. No inline handlers: everything is wired here (strict CSP).

const form = document.getElementById("course-form");

if (form) {
  // Klasse/Alter: show the inputs of the chosen kind only; at a school only Schulklasse/Lehrkräfte.
  const classAge = form.querySelector(".class-age");
  const schoolPlaces = JSON.parse(form.dataset.schoolPlaces || "{}");

  function updateKind() {
    if (!classAge) return;
    const place = form.elements.place_id?.value;
    const atSchool = Boolean(place) && schoolPlaces[place] === true;
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
    if (event.target.name === "class_kind" || event.target.name === "place_id") updateKind();
  });
  // The place picker is swapped by htmx after a choice or a new proposal.
  document.body.addEventListener("htmx:afterSwap", updateKind);
  updateKind();

  // Remove buttons for alternative dates and municipality rows.
  form.addEventListener("click", (event) => {
    if (event.target.matches("[data-remove]")) event.target.closest(".alt-row, tr").remove();
  });

  const addAlternative = form.querySelector("[data-add-alternative]");
  addAlternative?.addEventListener("click", () => {
    const row = document.createElement("span");
    row.className = "alt-row";
    const input = document.createElement("input");
    input.type = "date";
    input.name = "alt_date";
    input.setAttribute("aria-label", "Alternativdatum");
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "link-button";
    remove.dataset.remove = "";
    remove.textContent = "entfernen";
    row.append(input, " ", remove);
    addAlternative.before(row);
    input.focus();
  });

  const addMunicipality = form.querySelector("[data-add-municipality]");
  addMunicipality?.addEventListener("click", () => {
    const row = document.createElement("tr");
    row.innerHTML =
      '<td><input name="origin_name" list="dl-municipality" aria-label="Gemeinde"></td>' +
      '<td><input type="number" min="0" name="origin_count" aria-label="Teilnehmende"></td>' +
      '<td><button type="button" class="link-button" data-remove>entfernen</button></td>';
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
}

// "Termine hinzufügen" (WM-111): show the fields of the chosen rhythm; add and remove date rows.
const addForm = document.querySelector("[data-add-termine]");

if (addForm) {
  function updateRhythm() {
    const rhythm = addForm.querySelector('input[name="rhythmus"]:checked')?.value;
    const dates = rhythm === "daten";
    for (const block of addForm.querySelectorAll("[data-rhythm]")) {
      const on = (block.dataset.rhythm === "dates") === dates;
      block.hidden = !on;
      for (const input of block.querySelectorAll("input")) input.disabled = !on;
    }
  }
  addForm.addEventListener("change", (event) => {
    if (event.target.name === "rhythmus") updateRhythm();
  });
  addForm.addEventListener("click", (event) => {
    if (event.target.matches("[data-remove]")) event.target.closest(".alt-row").remove();
  });
  const addDate = addForm.querySelector("[data-add-date]");
  addDate?.addEventListener("click", () => {
    const row = document.createElement("span");
    row.className = "alt-row";
    const input = document.createElement("input");
    input.type = "date";
    input.name = "datum";
    input.setAttribute("aria-label", "Datum");
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "link-button";
    remove.dataset.remove = "";
    remove.textContent = "entfernen";
    row.append(input, " ", remove);
    addDate.before(row);
    input.focus();
  });
  updateRhythm();
}
