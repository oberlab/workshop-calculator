// Pickers (WM-101): a text field with suggestions instead of the select. Typing filters the entries
// (every word, ignoring case and accents, in the name and in a place's address and municipality);
// arrow keys and Enter or the mouse choose; when no entry is named exactly so, the last suggestion
// proposes the typed name as a new entry (the proposal fields open with it). The select stays,
// hidden, and holds the value, so forms, the htmx swap after a choice and planning.js work as
// before; without JavaScript the select and its "Neu vorschlagen" button are the picker.
// No inline handlers (strict CSP).

(function () {
  const SHOWN = 50; // suggestions at most; the list scrolls
  let count = 0;

  const fold = (text) =>
    (text || "")
      .normalize("NFD")
      .replace(/[̀-ͯ]/g, "")
      .toLowerCase();
  const compact = (text) => fold(text).replace(/[^a-z0-9]/g, "");
  // "Löten (Engineering)" and "Ort (vorgeschlagen)": the name without what is added in brackets.
  const bare = (text) => compact(text.replace(/\s*\([^)]*\)\s*$/, "").replace(/\s*\([^)]*\)\s*$/, ""));

  function enhance(picker) {
    const select = picker.querySelector("select");
    if (!select || picker.dataset.enhanced) return;
    picker.dataset.enhanced = "1";
    const id = `combo-${++count}`;
    const entries = [...select.options]
      .filter((option) => option.value)
      .map((option) => ({
        value: option.value,
        text: option.text,
        search: fold(`${option.text} ${option.dataset.search || ""}`),
        name: bare(option.text),
      }));

    const input = document.createElement("input");
    input.type = "text";
    input.className = "combo-input";
    input.autocomplete = "off";
    input.spellcheck = false;
    input.placeholder = `${picker.dataset.kind || "Eintrag"} suchen …`;
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-controls", `${id}-list`);
    const list = document.createElement("ul");
    list.id = `${id}-list`;
    list.className = "combo-list";
    list.setAttribute("role", "listbox");
    list.hidden = true;
    const box = document.createElement("div");
    box.className = "combo";
    box.append(input, list);
    select.before(box); // inside the label, before the select: the label now names the input
    select.hidden = true;
    select.tabIndex = -1;
    select.setAttribute("aria-hidden", "true");
    const newButton = picker.querySelector("[data-picker-new]");
    if (newButton) newButton.hidden = true;

    const chosenText = () => (select.value ? select.selectedOptions[0].text : "");
    input.value = chosenText();
    let items = [];
    let active = -1;

    function open(on) {
      list.hidden = !on;
      input.setAttribute("aria-expanded", on ? "true" : "false");
      if (!on) input.removeAttribute("aria-activedescendant");
    }

    function mark(index) {
      active = index;
      [...list.children].forEach((li, i) => li.classList.toggle("is-active", i === index));
      const li = list.children[index];
      if (li && li.id) {
        input.setAttribute("aria-activedescendant", li.id);
        li.scrollIntoView({ block: "nearest" });
      } else input.removeAttribute("aria-activedescendant");
    }

    function render() {
      const typed = input.value.trim();
      const words = fold(typed).split(/\s+/).filter(Boolean);
      const key = compact(typed);
      const rank = (e) => (e.name === key ? 0 : key && e.name.startsWith(key) ? 1 : 2);
      const found = entries
        .filter((e) => words.every((word) => e.search.includes(word)))
        .sort((a, b) => rank(a) - rank(b) || a.text.localeCompare(b.text, "de"));
      items = found.slice(0, SHOWN).map((e) => ({ ...e, isNew: false }));
      const exact = key && entries.some((e) => e.name === key || compact(e.text) === key);
      if (picker.dataset.newUrl && typed && !exact) {
        items.push({ isNew: true, typed, text: `„${typed}“ neu vorschlagen` });
      }
      const rows = items.map((item, i) => {
        const li = document.createElement("li");
        li.id = `${id}-${i}`;
        li.setAttribute("role", "option");
        li.textContent = item.text;
        if (item.isNew) li.className = "combo-new";
        if (!item.isNew && item.value === select.value) li.setAttribute("aria-selected", "true");
        // mousedown, not click: the input keeps the focus, so its blur does not close the list first.
        li.addEventListener("mousedown", (event) => {
          event.preventDefault();
          choose(i);
        });
        return li;
      });
      if (!rows.length) {
        const li = document.createElement("li");
        li.className = "combo-empty";
        li.textContent = "Nichts gefunden";
        rows.push(li);
      }
      list.replaceChildren(...rows);
      open(true);
      mark(items.length && typed ? 0 : -1);
    }

    function choose(index) {
      const item = items[index];
      if (!item) return;
      open(false);
      if (item.isNew) {
        // The proposal fields, with the typed name; the current choice stays until it is proposed.
        const url =
          `${picker.dataset.newUrl}&name=${encodeURIComponent(item.typed)}` +
          `&${encodeURIComponent(select.name)}=${encodeURIComponent(select.value)}`;
        htmx.ajax("GET", url, { target: picker, swap: "outerHTML" });
        return;
      }
      input.value = item.text;
      if (select.value !== item.value) {
        select.value = item.value;
        select.dispatchEvent(new Event("change", { bubbles: true })); // htmx shows the entry's details
      }
    }

    input.addEventListener("input", render);
    input.addEventListener("click", () => {
      if (list.hidden) render();
    });
    input.addEventListener("focus", () => input.select());
    input.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        if (list.hidden) render();
        else mark(Math.min(active + 1, items.length - 1));
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        mark(Math.max(active - 1, 0));
      } else if (event.key === "Enter") {
        event.preventDefault(); // never sends the surrounding form from here
        if (!list.hidden && active >= 0) choose(active);
      } else if (event.key === "Escape" && !list.hidden) {
        event.preventDefault();
        open(false);
        input.value = chosenText();
      }
    });
    input.addEventListener("blur", () => {
      open(false);
      if (!input.value.trim() && select.value) {
        select.value = ""; // emptied on purpose: no entry chosen
        select.dispatchEvent(new Event("change", { bubbles: true }));
      } else {
        input.value = chosenText();
      }
    });
    if (select.hasAttribute("autofocus")) input.focus(); // after an htmx swap, as the select had it
  }

  function enhanceIn(root) {
    if (root.matches && root.matches("[data-combobox]")) enhance(root);
    if (root.querySelectorAll) root.querySelectorAll("[data-combobox]").forEach(enhance);
  }

  if (window.htmx) htmx.onLoad(enhanceIn); // the page, and every picker htmx swaps in
  enhanceIn(document);
})();
