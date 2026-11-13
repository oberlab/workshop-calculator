// Places page: the Schulart field only for schools.
for (const form of document.querySelectorAll(".place-form")) {
  form.addEventListener("change", (event) => {
    if (event.target.name !== "school") return;
    form.querySelector("[data-school-only]").hidden = event.target.value !== "ja";
  });
}
