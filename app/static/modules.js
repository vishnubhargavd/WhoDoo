// Booking, paying and scanning for the Memberships, Tickets and Check-in pages.
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
  const button = event.target.closest("[data-pay-membership], [data-pay-booking], [data-admit]");
  if (!button) return;
  event.preventDefault();
  button.disabled = true;
  try {
    if (button.hasAttribute("data-pay-membership")) await payMembership();
    else if (button.dataset.payBooking) await payBooking(button.dataset.payBooking);
    else {
      const door = document.querySelector("[data-door]");
      showScan(await api("POST", "/api/v1/check-in/manual", { event_id: door.dataset.door, ticket_id: button.dataset.admit }));
      setTimeout(() => window.location.reload(), 1200);
    }
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

// --- The door ------------------------------------------------------------------------

const SCAN_TEXT = {
  redeemed: ["ok", "Checked in"],
  already_checked_in: ["warn", "Already checked in"],
  wrong_event: ["bad", "Ticket is for a different event"],
  revoked: ["bad", "Ticket is cancelled"],
  expired: ["bad", "This event is not admitting"],
  unrecognized: ["bad", "Not a ticket we issued"],
  invalid_credential: ["bad", "Not a WhoDoo ticket code"],
};

function showScan(result) {
  const box = document.getElementById("scan-result");
  if (!box) return;
  const [kind, text] = SCAN_TEXT[result.status] || ["bad", "Could not check this ticket"];
  box.className = "scan-result " + kind;
  box.hidden = false;
  box.querySelector("strong").textContent = text;
  const who = result.holderName ? `${result.holderName} · ${result.categoryName} · seat ${result.seatNumber} of ${result.partySize}` : "";
  const when = result.status === "already_checked_in" ? ` · at ${new Date(result.checkedInAt).toLocaleTimeString()}` : "";
  box.querySelector("span").textContent = who + when;
  if (result.status === "redeemed") {
    const count = document.getElementById("door-count");
    if (count) count.textContent = String(Number(count.textContent) + 1);
    const row = document.querySelector(`[data-ticket="${result.redemptionId}"]`);
    if (row) { row.querySelector(".badge").textContent = "Checked in"; row.querySelector(".badge").className = "badge paid"; const b = row.querySelector("button"); if (b) b.remove(); }
  }
}

async function submitScan(value) {
  const door = document.querySelector("[data-door]");
  try { showScan(await api("POST", "/api/v1/check-in/scan", { event_id: door.dataset.door, credential: value })); }
  catch (error) { showMessage(error.message); }
}

document.addEventListener("DOMContentLoaded", () => {
  const door = document.querySelector("[data-door]");
  if (!door) return;
  const manual = document.getElementById("scan-manual");
  manual.addEventListener("submit", (event) => {
    event.preventDefault();
    const field = manual.querySelector("input");
    if (field.value.trim()) submitScan(field.value.trim());
    field.value = "";
    field.focus();
  });

  const start = document.getElementById("scan-start");
  const video = document.getElementById("scan-video");
  const note = document.getElementById("scan-note");
  if (!("BarcodeDetector" in window) || !navigator.mediaDevices) {
    start.hidden = true;
    note.textContent = "This browser cannot read QR codes from the camera. Type or paste the code below, use a handheld scanner, or check people in from the guest list.";
    return;
  }
  start.addEventListener("click", async () => {
    try {
      video.srcObject = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } });
    } catch (error) {
      note.textContent = "The camera could not be opened. Allow camera access, or type the code below.";
      return;
    }
    await video.play();
    video.hidden = false;
    start.hidden = true;
    const detector = new BarcodeDetector({ formats: ["qr_code"] });
    let last = "", lastAt = 0;
    const tick = async () => {
      if (!video.srcObject) return;
      try {
        const codes = await detector.detect(video);
        const value = codes.length ? codes[0].rawValue : "";
        // The same code held in front of the camera is sent once, not on every frame.
        if (value && (value !== last || Date.now() - lastAt > 4000)) {
          last = value; lastAt = Date.now();
          await submitScan(value);
        }
      } catch (error) { /* a frame that could not be read; try the next */ }
      setTimeout(tick, 250);
    };
    tick();
  });
});
