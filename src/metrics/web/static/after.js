// Phone form after the course (WM-133): plus and minus buttons next to the number fields and the
// running sums. No inline handlers (strict CSP); without JavaScript the buttons stay hidden and the
// number fields are sent as typed.

const afterForm = document.querySelector("[data-after-form]");

if (afterForm) {
  const value = (input) => Math.max(0, parseInt(input.value, 10) || 0);
  const sum = (selector) => [...afterForm.querySelectorAll(selector)].reduce((n, i) => n + value(i), 0);
  const fixedTotal = afterForm.querySelector("[data-fixed-total]");
  const countTotal = afterForm.querySelector("[data-count-total]");
  const originSum = afterForm.querySelector("[data-origin-sum]");

  function update() {
    const total = fixedTotal ? parseInt(fixedTotal.dataset.fixedTotal, 10) || 0 : sum("[data-count]");
    if (countTotal) countTotal.textContent = total;
    if (!originSum) return;
    const origins = sum("[data-origin]");
    // Nothing entered yet: a neutral hint, not a warning about a sum of 0 of 0.
    const empty = total === 0 && origins === 0;
    originSum.classList.toggle("is-neutral", empty);
    originSum.classList.toggle("is-ok", origins === total && total > 0);
    if (empty) {
      originSum.textContent = "Erst die Teilnehmenden eintragen, dann hier die Herkunft.";
    } else if (origins === total && total > 0) {
      originSum.textContent = `Summe passt: ${origins} von ${total}`;
    } else if (origins <= total) {
      originSum.textContent = `Herkunft: ${origins} von ${total}, es fehlen ${total - origins}`;
    } else {
      originSum.textContent = `Herkunft: ${origins} von ${total}, ${origins - total} zu viel`;
    }
  }

  for (const button of afterForm.querySelectorAll("[data-step]")) button.hidden = false;
  afterForm.addEventListener("click", (event) => {
    const button = event.target.closest("[data-step]");
    if (!button) return;
    const input = button.closest("[data-stepper]").querySelector("input");
    input.value = Math.max(0, value(input) + parseInt(button.dataset.step, 10));
    update();
  });
  afterForm.addEventListener("input", update);
  // Tapping a number selects it, so typing replaces the 0 instead of appending to it.
  afterForm.addEventListener("focusin", (event) => {
    if (event.target.matches('input[type="number"]')) event.target.select();
  });
  update();
}
