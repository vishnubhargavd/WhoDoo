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

function showMessage(text, kind) {
  const box = document.getElementById("flash");
  box.textContent = text;
  box.className = "notice " + (kind || "error");
  box.hidden = false;
  box.scrollIntoView({ block: "nearest" });
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
function watchForChanges(url, seconds) {
  let last = null;
  async function check() {
    if (document.hidden) return;
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
document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-post], [data-put]");
  if (!button) return;
  event.preventDefault();
  const body = button.dataset.body ? JSON.parse(button.dataset.body) : {};
  if (button.dataset.prompt) {
    const answer = window.prompt(button.dataset.prompt);
    if (answer === null) return;
    if (button.hasAttribute("data-required") && !answer.trim()) {
      showMessage(button.dataset.prompt + " is required.");
      return;
    }
    if (answer.trim()) body[button.dataset.field || "comment"] = answer.trim();
  } else if (button.dataset.confirm && !window.confirm(button.dataset.confirm)) {
    return;
  }
  button.disabled = true;
  try {
    const result = button.dataset.put
      ? await api("PUT", button.dataset.put, body)
      : await api("POST", button.dataset.post, body);
    finish(button, result);
  } catch (error) {
    showMessage(error.message);
    button.disabled = false;
  }
});
