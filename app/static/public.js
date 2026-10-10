// Registering for an event and paying for a held place, without an account.
// Uses api(), showMessage() and formToJson() from app.js.

document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (!form.dataset.register) return;
  event.preventDefault();
  if (!form.reportValidity()) return;
  const button = form.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    const body = formToJson(form);
    if (!("website" in body)) body.website = "";
    const registration = await api("POST", form.dataset.register, body);
    // The ticket page shows the QR code, or the payment step for a paid event.
    window.location.href = new URL(registration.ticket_url).pathname;
  } catch (error) {
    showMessage(error.message);
    button.disabled = false;
  }
});

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-pay-registration], [data-decline-registration]");
  if (!button) return;
  const card = document.querySelector("[data-ticket-token]");
  const base = "/api/v1/public/registrations/" + card.dataset.ticketToken;
  button.disabled = true;
  try {
    const checkout = await api("POST", base + "/checkout");
    if (checkout.checkout_url) { window.location.href = checkout.checkout_url; return; }
    // Mock provider: there is no hosted page, so the outcome is chosen here.
    const outcome = button.hasAttribute("data-decline-registration") ? "failure" : "success";
    const result = await api("POST", base + "/mock", { outcome });
    if (outcome === "failure") showMessage("The card was declined. Nothing was charged; you can try again.");
    else if (result.outcome !== "registered") showMessage("The payment could not be applied: " + result.outcome.replace(/_/g, " ") + ".");
    if (outcome === "success") { window.location.reload(); return; }
  } catch (error) {
    showMessage(error.message);
  }
  button.disabled = false;
});
