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

function finish(element) {
  const next = element.dataset.next;
  if (next) window.location.href = next;
  else window.location.reload();
}

// <form data-api="POST" action="/api/v1/..." data-next="/somewhere">
document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (!form.dataset.api) return;
  event.preventDefault();
  const button = form.querySelector("button[type=submit], button:not([type])");
  if (button) button.disabled = true;
  try {
    await api(form.dataset.api, form.getAttribute("action"), formToJson(form));
    finish(form);
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
    if (button.dataset.put) await api("PUT", button.dataset.put, body);
    else await api("POST", button.dataset.post, body);
    finish(button);
  } catch (error) {
    showMessage(error.message);
    button.disabled = false;
  }
});
