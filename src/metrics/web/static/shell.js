// App shell: htmx settings, the menu on phones, toasts and side panels. Loaded with defer on every
// page, right after htmx, so the settings apply before htmx starts.

// Under the strict CSP: no eval (hx-on, js: values) and no injected <style> (style.css has the
// indicator rules); script tags in swapped content would be blocked anyway.
if (window.htmx) {
  htmx.config.allowEval = false;
  htmx.config.allowScriptTags = false;
  htmx.config.includeIndicatorStyles = false;
}

// Menu on phones: the button shows and hides the sidebar; Escape closes it again.
const menuButton = document.querySelector("[data-menu-toggle]");
const sidebar = document.getElementById(menuButton?.getAttribute("aria-controls") || "");

function setMenu(open) {
  menuButton.setAttribute("aria-expanded", String(open));
  sidebar.classList.toggle("is-open", open);
}

if (menuButton && sidebar) {
  menuButton.addEventListener("click", () => setMenu(menuButton.getAttribute("aria-expanded") !== "true"));
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !sidebar.classList.contains("is-open")) return;
    const inside = sidebar.contains(document.activeElement);
    setMenu(false);
    if (inside) menuButton.focus();
  });
}

// Toasts: closed with their button; htmx responses add one with HX-Trigger: {"toast": "…"}.
const toasts = document.querySelector("[data-toasts]");

function showToast(message) {
  const toast = document.createElement("div");
  toast.className = "toast";
  toast.setAttribute("role", "status");
  const text = document.createElement("p");
  text.textContent = message;
  const close = document.createElement("button");
  close.type = "button";
  close.className = "toast-close";
  close.dataset.closeToast = "";
  close.setAttribute("aria-label", "Meldung schließen");
  close.textContent = "×";
  toast.append(text, close);
  toasts.append(toast);
}

document.addEventListener("click", (event) => {
  const close = event.target.closest("[data-close-toast]");
  if (close) close.closest(".toast").remove();
});
document.body.addEventListener("toast", (event) => {
  const message = event.detail?.value ?? event.detail;
  if (typeof message === "string" && toasts) showToast(message);
});

// Side panels (<dialog class="side-panel">): opened by [data-side-panel="<id>"], closed by
// [data-close-dialog], Escape or a click on the backdrop. The browser keeps focus inside.
document.addEventListener("click", (event) => {
  const opener = event.target.closest("[data-side-panel]");
  if (opener) {
    document.getElementById(opener.dataset.sidePanel)?.showModal();
    return;
  }
  if (event.target.closest("[data-close-dialog]")) {
    event.target.closest("dialog")?.close();
    return;
  }
  if (event.target.matches("dialog.side-panel")) event.target.close();
});
