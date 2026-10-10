// FairSeat ticket settings page (Member 2). Sends the whole configuration as one PUT,
// so the server checks the seat split as a unit. Uses api()/showMessage() from app.js.

(function () {
  const form = document.getElementById("ticket-form");
  if (!form) return;
  const body = form.querySelector(".ticket-categories tbody");
  const template = document.getElementById("category-row");
  const allocation = document.getElementById("allocation");

  // "250", "250.5" or "250.50" rupees -> 25050 paise, using string arithmetic (no floating point).
  function toPaise(text) {
    const match = /^(\d{1,6})(?:\.(\d{1,2}))?$/.exec(text.trim());
    if (!match) return null;
    return Number(match[1]) * 100 + Number((match[2] || "0").padEnd(2, "0"));
  }

  function rows() {
    return Array.from(body.querySelectorAll("tr"));
  }

  function showAllocation() {
    const total = Number(form.elements.total_capacity.value) || 0;
    const used = rows().reduce((sum, row) => sum + (Number(row.querySelector("[name=capacity]").value) || 0), 0);
    allocation.textContent = `${used} of ${total} seats are in categories` +
      (used === total ? "." : used < total ? ` (${total - used} unallocated).` : ` (${used - total} too many).`);
  }

  form.addEventListener("click", (event) => {
    if (event.target.closest("[data-add-row]")) {
      body.appendChild(template.content.cloneNode(true));
      body.lastElementChild.querySelector("input").focus();
      showAllocation();
    } else if (event.target.closest("[data-remove-row]")) {
      event.target.closest("tr").remove();
      showAllocation();
    }
  });
  form.addEventListener("input", showAllocation);
  showAllocation();

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const categories = [];
    for (const row of rows()) {
      const name = row.querySelector("[name=name]").value.trim();
      const price = toPaise(row.querySelector("[name=price]").value);
      if (price === null) {
        showMessage(`Enter ${name || "each category"}'s price in rupees, like 250 or 250.50.`);
        return;
      }
      const category = {
        name,
        price_paise: price,
        capacity: Number(row.querySelector("[name=capacity]").value),
        member_discount_eligible: row.querySelector("[name=member_discount_eligible]").checked,
        is_visible: row.querySelector("[name=is_visible]").checked,
      };
      if (row.dataset.id) category.id = row.dataset.id;
      categories.push(category);
    }
    const payload = {
      sales_enabled: form.elements.sales_enabled.checked,
      capacity: Number(form.elements.total_capacity.value),
      opens_at: form.elements.opens_at.value,
      closes_at: form.elements.closes_at.value,
      max_per_booking: Number(form.elements.max_per_booking.value),
      categories,
    };
    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      await api("PUT", form.getAttribute("action"), payload);
      sessionStorage.setItem("flash", "Ticket settings saved.");
      window.location.reload();
    } catch (error) {
      showMessage(error.message);
      button.disabled = false;
    }
  });
})();
