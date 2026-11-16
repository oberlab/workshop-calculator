// Tabelle (WM-152): the form is a plain GET form; this only shows the date fields for "Eigener
// Zeitraum" and keeps the "(n ausgewählt)" counters of the checklists up to date.

const tableForm = document.getElementById("table-filters");

function updateTableForm() {
  const custom = tableForm.period.value === "custom";
  for (const el of tableForm.querySelectorAll(".custom-range")) el.hidden = !custom;
  for (const details of tableForm.querySelectorAll(".checklist")) {
    const checked = details.querySelectorAll("input:checked").length;
    const selection = details.querySelector(".selection");
    selection.textContent = checked ? `(${checked} ausgewählt)` : `(${selection.dataset.none})`;
  }
}

if (tableForm) {
  tableForm.addEventListener("change", updateTableForm);
  updateTableForm();
}
