// Booking and paying for the Memberships and Tickets pages. (The door has its own script, door.js.)
// Uses api(), showMessage() and askDialog() from app.js.

function rupees(paise) {
  return "₹" + (paise / 100).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// With the mock provider there is no hosted page, so the choice Stripe's page would offer is asked here.
async function mockOutcome(amount) {
  const pay = await askDialog({
    title: "Mock payment",
    message: `No real money moves. Pay ${rupees(amount)} now? Cancel leaves it unpaid.`,
    confirmLabel: "Pay " + rupees(amount),
  });
  return pay === null ? null : "success";
}

async function payMembership() {
  const checkout = await api("POST", "/api/v1/memberships/checkout");
  if (checkout.checkout_url) { window.location.href = checkout.checkout_url; return; }
  const outcome = await mockOutcome(checkout.amount);
  if (!outcome) return;
  await api("POST", "/api/v1/memberships/checkout/mock", { session_id: checkout.session_id, outcome });
  sessionStorage.setItem("flash", "Payment received. Your membership is active.");
  window.location.reload();
}

async function payBooking(bookingId) {
  const checkout = await api("POST", "/api/v1/ticket-payments/stripe/create-checkout-session", { booking_id: bookingId });
  if (checkout.checkout_url) { window.location.href = checkout.checkout_url; return; }
  const outcome = await mockOutcome(checkout.amount);
  if (!outcome) { window.location.reload(); return; }
  await api("POST", "/api/v1/ticket-payments/mock/simulate", { session_id: checkout.session_id, outcome });
  await api("POST", "/api/v1/ticket-payments/stripe/verify", { session_id: checkout.session_id });
  sessionStorage.setItem("flash", "Payment received. Your tickets are ready.");
  window.location.href = "/tickets/mine";
}

function newKey() {
  return (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2)).replace(/[^A-Za-z0-9_-]/g, "") + "booking";
}

async function bookTickets(form) {
  const request = {
    event_id: form.dataset.event,
    ticket_category_id: form.dataset.category,
    quantity: Number(form.querySelector("[name=quantity]").value),
  };
  // The price is the server's: a quote first, shown to the buyer, then the booking that accepts it.
  const quote = await api("POST", "/api/v1/ticket-bookings/quotes", request);
  const lines = [`${quote.quantity} × ${rupees(quote.unit_price_paise)} = ${rupees(quote.total_price_paise)}.`];
  if (quote.discount_applied) lines.push(`Member price (was ${rupees(quote.base_unit_price_paise)} each).`);
  lines.push(quote.total_price_paise > 0 ? "Seats are held for 10 minutes while you pay." : "These tickets are free.");
  const agreed = await askDialog({ title: "Book " + form.dataset.name, message: lines.join(" "), confirmLabel: "Book" });
  if (agreed === null) return;
  const booking = await api("POST", "/api/v1/ticket-bookings", { ...request, idempotency_key: newKey(), accepted_quote_id: quote.id });
  if (booking.status === "confirmed") {
    sessionStorage.setItem("flash", "Booked. Your tickets are ready.");
    window.location.href = "/tickets/mine";
  } else if (document.querySelector("[data-ticket-payments=on]")) {
    await payBooking(booking.booking_id);
  } else {
    window.location.reload();
  }
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-pay-membership], [data-pay-booking]");
  if (!button) return;
  event.preventDefault();
  button.disabled = true;
  try {
    if (button.hasAttribute("data-pay-membership")) await payMembership();
    else await payBooking(button.dataset.payBooking);
  } catch (error) {
    showMessage(error.message);
  }
  button.disabled = false;
});

document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (!form.matches("[data-book]")) return;
  event.preventDefault();
  const button = form.querySelector("button");
  button.disabled = true;
  try { await bookTickets(form); } catch (error) { showMessage(error.message); }
  button.disabled = false;
});
