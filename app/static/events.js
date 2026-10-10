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

// <select data-assign="/api/v1/tasks/..." data-event data-task data-title>: empty value unassigns.
// A new owner is reviewed for duplicates and clashes first (reviewAssignment, below).
document.addEventListener("change", async (event) => {
  const select = event.target.closest("select[data-assign]");
  if (!select) return;
  select.disabled = true;
  try {
    let body = select.value ? { assignee_id: select.value } : { unassign: true };
    if (select.value && select.dataset.event) {
      body = await reviewAssignment(select.dataset.event, { ...body, title: select.dataset.title },
                                    peopleNames(), select.dataset.task);
      if (body) delete body.title;
    }
    if (body) {
      await api("PATCH", select.dataset.assign, body);
      if (body.override_reason) sessionStorage.setItem("flash", "Assigned. Override recorded.");
    }
    window.location.reload();
  } catch (error) {
    showMessage(error.message);
    select.disabled = false;
  }
});

// --- CrewPulse assignment review (duplicates and clashes) ------------------------------------
// Before a task is given to someone, ask the server what it finds. Clean: save. Flagged: show
// the issues, offer people who are free instead, or save anyway with a reason (never for an
// exact duplicate). The server checks again when saving, so this is guidance, not the gate.

function reviewDialog(review, currentName) {
  return new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.className = "modal";
    const form = document.createElement("form");
    form.method = "dialog";
    form.className = "stack";
    const heading = document.createElement("h2");
    heading.textContent = review.can_override ? "Review before assigning" : "This is a duplicate";
    const intro = document.createElement("p");
    intro.textContent = `Found for ${currentName}:`;
    const list = document.createElement("ul");
    for (const issue of review.issues) {
      const item = document.createElement("li");
      item.textContent = issue.message;
      if (issue.blocking) item.style.fontWeight = "600";
      list.append(item);
    }
    form.append(heading, intro, list);

    let answer = null;
    if (review.suggestions.length) {
      const label = document.createElement("p");
      label.textContent = "Free at that time instead:";
      const choices = document.createElement("div");
      choices.className = "actions";
      for (const person of review.suggestions) {
        const pick = document.createElement("button");
        pick.type = "button";
        pick.className = "small secondary";
        pick.textContent = `${person.full_name} · ${person.open_tasks} open`;
        pick.addEventListener("click", () => { answer = { assignee_id: person.id }; dialog.close(); });
        choices.append(pick);
      }
      form.append(label, choices);
    }

    let reason = null;
    if (review.can_override) {
      const label = document.createElement("label");
      label.textContent = "Or assign anyway. Reason (kept in the audit log):";
      reason = document.createElement("textarea");
      reason.rows = 2;
      reason.minLength = 5;
      reason.maxLength = 500;
      reason.required = true;
      label.append(reason);
      form.append(label);
    }
    const actions = document.createElement("div");
    actions.className = "actions";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "secondary";
    cancel.textContent = review.can_override ? "Cancel and fix" : "Close";
    cancel.addEventListener("click", () => dialog.close());
    actions.append(cancel);
    if (review.can_override) {
      const anyway = document.createElement("button");
      anyway.type = "submit";
      anyway.className = "danger";
      anyway.textContent = "Assign anyway";
      actions.append(anyway);
    }
    form.append(actions);
    form.addEventListener("submit", () => { answer = { override_reason: reason.value.trim() }; });
    dialog.append(form);
    document.body.append(dialog);
    dialog.addEventListener("close", () => { dialog.remove(); resolve(answer); });
    dialog.showModal();
  });
}

// Review `body.assignee_id` for this task. Returns the body to save, or null if the organizer backed out.
async function reviewAssignment(eventId, body, names, taskId) {
  let current = { ...body };
  for (;;) {
    if (!current.assignee_id) return current;
    const review = await api("POST", `/api/v1/events/${eventId}/tasks/review`, {
      title: current.title, assignee_id: current.assignee_id, task_id: taskId || null,
      shift_starts_at: current.shift_starts_at || null, shift_ends_at: current.shift_ends_at || null,
    });
    if (review.ok) return current;
    const choice = await reviewDialog(review, names[current.assignee_id] || "this person");
    if (!choice) return null;
    if (choice.override_reason) return { ...current, override_reason: choice.override_reason };
    current = { ...current, assignee_id: choice.assignee_id };   // a suggestion: review that person too
  }
}

function peopleNames() {
  const names = {};
  document.querySelectorAll("select[name=assignee_id] option").forEach((option) => {
    if (option.value) names[option.value] = option.textContent.replace(/\s*\(.*\)$/, "");
  });
  return names;
}

// <form data-task-form data-event="..."> : add a task, reviewed first.
document.addEventListener("submit", async (event) => {
  const form = event.target.closest("form[data-task-form]");
  if (!form) return;
  event.preventDefault();
  const button = form.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    const body = await reviewAssignment(form.dataset.event, formToJson(form), peopleNames());
    if (body) {
      await api("POST", form.getAttribute("action"), body);
      sessionStorage.setItem("flash", body.override_reason ? "Task added. Override recorded." : "Task added.");
      window.location.reload();
      return;
    }
  } catch (error) {
    showMessage(error.message);
  }
  button.disabled = false;
});
