// Member 2 pages. Adds PATCH buttons and assignee selects on top of app.js helpers.

// <button data-patch="/api/v1/tasks/..." data-body='{"unassign": true}'>
document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-patch]");
  if (!button) return;
  event.preventDefault();
  button.disabled = true;
  try {
    await api("PATCH", button.dataset.patch, JSON.parse(button.dataset.body || "{}"));
    finish(button);
  } catch (error) {
    showMessage(error.message);
    button.disabled = false;
  }
});

// <select data-assign="/api/v1/tasks/..."> : empty value unassigns.
document.addEventListener("change", async (event) => {
  const select = event.target.closest("select[data-assign]");
  if (!select) return;
  select.disabled = true;
  const body = select.value ? { assignee_id: select.value } : { unassign: true };
  try {
    await api("PATCH", select.dataset.assign, body);
    window.location.reload();
  } catch (error) {
    showMessage(error.message);
    select.disabled = false;
  }
});
