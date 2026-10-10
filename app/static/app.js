// Minimal helpers so the review pages can call the JSON API. No framework.

async function api(method, url, body) {
  const options = { method, headers: {}, credentials: "same-origin" };
  if (body instanceof FormData) {
    options.body = body;
  } else if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(url, options);
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error((data && data.error && data.error.message) || `Request failed (${response.status})`);
  }
  return data;
}

// A toast at the bottom of the screen. Errors stay until the next message; confirmations fade.
let toastTimer = null;
function showMessage(text, kind) {
  const box = document.getElementById("flash");
  if (!box) return;
  box.hidden = true; // restart the entrance animation
  box.textContent = text;
  box.className = "notice " + (kind || "error");
  void box.offsetWidth;
  box.hidden = false;
  clearTimeout(toastTimer);
  if (kind === "ok") toastTimer = setTimeout(() => { box.hidden = true; }, 6000);
}

// A designed, accessible replacement for window.confirm and window.prompt.
// Resolves to null if cancelled, otherwise "" (confirm) or the text entered (prompt).
function askDialog({ title, message, input, required, confirmLabel, danger }) {
  return new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.className = "modal";
    const form = document.createElement("form");
    form.method = "dialog";
    form.className = "stack";
    const heading = document.createElement("h2");
    heading.textContent = title;
    const text = document.createElement(input ? "label" : "p");
    text.textContent = message;
    let field = null;
    if (input) {
      field = document.createElement("textarea");
      field.required = Boolean(required);
      field.minLength = required ? 3 : 0;
      field.rows = 3;
      text.append(field);
    }
    const actions = document.createElement("div");
    actions.className = "actions";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "secondary";
    cancel.textContent = "Cancel";
    const confirm = document.createElement("button");
    confirm.type = "submit";
    confirm.textContent = confirmLabel || "Confirm";
    if (danger) confirm.className = "danger";
    actions.append(cancel, confirm);
    form.append(heading, text, actions);
    dialog.append(form);
    document.body.append(dialog);

    let answer = null;
    cancel.addEventListener("click", () => dialog.close());
    form.addEventListener("submit", () => { answer = field ? field.value.trim() : ""; });
    dialog.addEventListener("close", () => { dialog.remove(); resolve(answer); });
    dialog.showModal();
    (field || confirm).focus();
  });
}

// Collect a form's fields as JSON, leaving out blanks so optional fields stay unset.
function formToJson(form) {
  const body = {};
  for (const [key, value] of new FormData(form).entries()) {
    if (typeof value === "string" && value.trim() !== "") body[key] = value.trim();
  }
  for (const box of form.querySelectorAll("input[type=checkbox][name]")) {
    body[box.name] = box.checked;
  }
  return body;
}

// After an action the page reloads; carry a one-line confirmation across the reload.
function finish(element, result) {
  if (result && result.status_label) {
    sessionStorage.setItem("flash", `${result.title}: ${result.status_label}.`);
  }
  const next = element.dataset.next;
  if (next) window.location.href = next;
  else window.location.reload();
}

document.addEventListener("DOMContentLoaded", () => {
  const message = sessionStorage.getItem("flash");
  if (message) {
    sessionStorage.removeItem("flash");
    showMessage(message, "ok");
  }
});

// Keep a page in step with the server: when what `url` returns changes (someone approved,
// paid, ordered...), reload. Skipped while the tab is hidden or the user is typing.
// The demo sign-in is one cookie per browser. If another tab signs in as someone else, this
// tab would silently start showing that person's data on its next refresh, which looks like
// "my claim vanished". Instead, say what happened and let the person choose.
let identityChanged = false;
async function checkIdentity() {
  const pageUser = document.body.dataset.userId;
  if (!pageUser || identityChanged) return identityChanged;
  try {
    const now = await fetch("/whoami", { credentials: "same-origin" }).then((response) => response.json());
    if (now.id === pageUser) return false;
    if (identityChanged) return true; // another check running at the same moment already said so
    identityChanged = true;
    const bar = document.createElement("div");
    bar.className = "notice info switched";
    bar.setAttribute("role", "alert");
    const text = document.createElement("span");
    text.textContent = now.id
      ? `This browser is now signed in as ${now.name}. This tab still shows ${document.body.dataset.userName}'s page. To use two people at once, open one of them in a private window.`
      : `You have been signed out in another tab. This page still shows ${document.body.dataset.userName}'s view.`;
    const reload = document.createElement("button");
    reload.type = "button";
    reload.className = "small";
    reload.textContent = now.id ? `Continue as ${now.name}` : "Sign in";
    reload.addEventListener("click", () => { window.location.href = now.id ? "/home" : "/login"; });
    bar.append(text, reload);
    document.querySelector("main").prepend(bar);
  } catch (error) { /* offline: check again later */ }
  return identityChanged;
}
if (document.body.dataset.userId) {
  setInterval(checkIdentity, 5000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) checkIdentity(); });
}

function watchForChanges(url, seconds) {
  let last = null;
  async function check() {
    if (document.hidden) return;
    if (await checkIdentity()) return; // never refresh into somebody else's data
    const active = document.activeElement;
    if (active && ["INPUT", "TEXTAREA", "SELECT"].includes(active.tagName)) return;
    try {
      const response = await fetch(url, { credentials: "same-origin" });
      if (!response.ok) return;
      const text = await response.text();
      if (last !== null && text !== last) window.location.reload();
      last = text;
    } catch (error) { /* offline for a moment: try again next time */ }
  }
  check(); // take the baseline straight away, so nothing that happens after the page loads is missed
  setInterval(check, (seconds || 5) * 1000);
  document.addEventListener("visibilitychange", check); // catch up as soon as the tab is looked at again
}

// <form data-api="POST" action="/api/v1/..." data-next="/somewhere">
document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (!form.dataset.api) return;
  event.preventDefault();
  const button = form.querySelector("button[type=submit], button:not([type])");
  if (button) button.disabled = true;
  try {
    const result = await api(form.dataset.api, form.getAttribute("action"), formToJson(form));
    finish(form, result);
  } catch (error) {
    showMessage(error.message);
    if (button) button.disabled = false;
  }
});

// <button data-post="/api/v1/..." data-prompt="Reason" data-field="reason" data-required>
// <button data-put="/api/v1/..." data-body='{"is_active": false}'>
// <button data-delete="/api/v1/..." data-confirm="...">
document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-post], [data-put], [data-delete]");
  if (!button) return;
  event.preventDefault();
  const body = button.dataset.body ? JSON.parse(button.dataset.body) : {};
  const label = button.textContent.trim();
  if (button.dataset.prompt) {
    const answer = await askDialog({
      title: label, message: button.dataset.prompt, input: true,
      required: button.hasAttribute("data-required"), confirmLabel: label,
      danger: button.classList.contains("danger"),
    });
    if (answer === null) return;
    if (answer) body[button.dataset.field || "comment"] = answer;
  } else if (button.dataset.confirm) {
    const agreed = await askDialog({
      title: label, message: button.dataset.confirm, confirmLabel: label,
      danger: button.classList.contains("danger"),
    });
    if (agreed === null) return;
  }
  button.disabled = true;
  try {
    const result = button.dataset.delete
      ? await api("DELETE", button.dataset.delete)
      : button.dataset.put
        ? await api("PUT", button.dataset.put, body)
        : await api("POST", button.dataset.post, body);
    finish(button, result);
  } catch (error) {
    showMessage(error.message);
    button.disabled = false;
  }
});

// --- Shell and motion ---------------------------------------------------------------

// The glass menu: toggle, close on Escape or an outside click, return focus to the toggle.
document.querySelectorAll("[data-menu]").forEach((wrap) => {
  const toggle = wrap.querySelector(".menu-toggle");
  const menu = wrap.querySelector(".site-menu");
  const set = (open) => {
    menu.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    toggle.setAttribute("aria-label", open ? "Close menu" : "Open menu");
  };
  toggle.addEventListener("click", () => set(menu.hidden));
  menu.addEventListener("click", (event) => { if (event.target.closest("a")) set(false); });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !menu.hidden) { set(false); toggle.focus(); }
  });
  document.addEventListener("pointerdown", (event) => { if (!wrap.contains(event.target)) set(false); });
});

const calmMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// Reveal on scroll.
const revealables = document.querySelectorAll(".reveal");
if (calmMotion || !("IntersectionObserver" in window)) {
  revealables.forEach((element) => element.classList.add("in"));
} else {
  const observer = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (entry.isIntersecting) { entry.target.classList.add("in"); observer.unobserve(entry.target); }
    }
  }, { rootMargin: "0px 0px -60px 0px" });
  revealables.forEach((element) => observer.observe(element));
}

if (!calmMotion) {
  // Hero parallax: the background and stickers drift with the pointer, at different depths.
  document.querySelectorAll("[data-parallax]").forEach((hero) => {
    hero.addEventListener("pointermove", (event) => {
      if (event.pointerType !== "mouse") return;
      const box = hero.getBoundingClientRect();
      hero.style.setProperty("--px", (((event.clientX - box.left) / box.width - 0.5) * 2).toFixed(3));
      hero.style.setProperty("--py", (((event.clientY - box.top) / box.height - 0.5) * 2).toFixed(3));
    });
    hero.addEventListener("pointerleave", () => { hero.style.setProperty("--px", 0); hero.style.setProperty("--py", 0); });
  });

  // Magnetic pills lean a little toward the pointer.
  document.querySelectorAll("[data-magnetic]").forEach((pill) => {
    const clamp = (value) => Math.max(-14, Math.min(14, value));
    pill.addEventListener("pointermove", (event) => {
      if (event.pointerType !== "mouse") return;
      const box = pill.getBoundingClientRect();
      pill.style.setProperty("--mx", clamp((event.clientX - (box.left + box.width / 2)) * 0.22).toFixed(1));
      pill.style.setProperty("--my", clamp((event.clientY - (box.top + box.height / 2)) * 0.22).toFixed(1));
    });
    pill.addEventListener("pointerleave", () => { pill.style.setProperty("--mx", 0); pill.style.setProperty("--my", 0); });
  });
}

// The round W on the landing page replays the intro.
document.querySelectorAll("[data-replay]").forEach((button) => {
  button.addEventListener("click", () => {
    window.scrollTo({ top: 0, behavior: calmMotion ? "auto" : "smooth" });
    const body = document.querySelector(".hero-body");
    if (!body || calmMotion) return;
    const fresh = body.cloneNode(true);
    body.replaceWith(fresh);
    fresh.querySelectorAll("[data-magnetic]").forEach((pill) => pill.style.removeProperty("--mx"));
  });
});
