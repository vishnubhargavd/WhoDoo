# Batch A (Member 2): payments, profiles, reminders, registrations

Branch `joshith/batch-a-enhancements`. Every new table sits in `migrations/pending/040-042`.
Those migrations are local only until the team approves them. Each feature hides itself
(it answers 404 or shows "not switched on") until its tables exist.

```
python -m scripts.migrate_pending     # local database only: adds 040, 041, 042
```

## 1. Razorpay, UPI ids, payouts

| Piece | Where | Status |
|---|---|---|
| Razorpay Payment Links as a third provider (dues, tickets, merch) | `app/services/razorpay_provider.py`, `payment_service.get_provider` | Tested with a fake HTTP transport. **Not run against a real Razorpay account.** |
| Webhook `POST /api/v1/payments/razorpay/webhook` | `app/api/v1/member2.py` | HMAC-SHA256 signature, event-id dedupe, server re-fetch before recording anything |
| Return `/payments/razorpay/return` | `app/web/member2_pages.py` | Same-site redirect only; the destination page verifies with Razorpay |
| UPI id on your profile `PUT/DELETE /api/v1/me/payment-profile` | `profile_service.py` | Stored encrypted (Fernet), shown masked, never logged |
| Reimbursement payouts `POST /api/v1/expenses/{id}/payout` | `payout_service.py` | Mock tested. RazorpayX code written to the docs, **not run against RazorpayX** |

Rules the code enforces:
- **Payment is verified by the server.** Something is marked paid only after the provider confirms it. A forged webhook body changes nothing, because each module reads the payment link back from Razorpay first.
- **Payouts need a fully approved claim.** Only an `approved` claim can be paid out, so the existing approval rule still applies: claims of ₹1,000 or more need two different approvers, and neither can be the claimant. The treasurer or president starts the payout, never for their own claim.
- **The UPI id must be verified.** The payee's UPI id must be marked verified by the payout provider. The mock provider marks verified any UPI id whose name part does not start with `fail`, so the failure case can be demonstrated.
- **Paid means processed.** The claim and its ledger entry become `paid` only when the provider reports the payout `processed`. Failed and reversed payouts leave the claim `approved` so it can be retried. While a payout is in flight, *Mark paid* and removing the UPI id are both refused.
- **No automatic debit.** Saving a UPI id never authorises a debit. Dues and tickets use the existing *Pay now* flows. Recurring UPI mandates (UPI AutoPay) are **not implemented**; they need Razorpay subscriptions and the member's explicit mandate.
- **Test keys only.** Only `rzp_test_` keys are used. An `rzp_live_` key stops the app at startup.

Before switching Razorpay on, check with a real test account:
1. Create a payment link, pay it with test UPI `success@razorpay`, and confirm `payment_link.paid` reaches the webhook and the payer is redirected back.
2. Confirm the webhook payload shape matches `translate_webhook`: `payload.payment_link.entity.notes`, and `amount_paid` in paise.
3. Run a refund through the FairSeat or MemberCore refund-due path.
4. RazorpayX (needs a RazorpayX account): confirm fund-account validation returns `results.account_status`, a payout reaches `processed`, and the payout webhooks arrive.

## 2. Profiles and renewal reminders

- **Profile.** `GET/PUT /api/v1/me/profile` and the `/profile` page. You can change your name, display name, phone, department and academic year. Role, email, membership status and expiry are refused: unknown fields get a 422. Profile photo upload is **not built**.
- **Reminders.** A reminder is written 30, 14, 7 and 1 days before expiry, and once on expiry. Only the stage currently reached is written; a unique key stops duplicates.
  - **When they're written:** by an hourly job in the app (`RENEWAL_REMINDER_INTERVAL_SECONDS`), whenever someone opens Home, Memberships or Notifications, or from cron with `python -m scripts.send_renewal_reminders`.
  - **Where they show:** Home, Memberships and the new `/notifications` page, each with a *Renew now* button.
  - **After renewal:** renewing changes the expiry, so the old term's reminders stop showing. Cancelled and pending memberships get none.

## 3. Event registrations, low tickets, budget

- **Already in FairSeat:** capacity, ticket categories with their own capacities, and registration dates. This batch adds:
  - a budget field on the event form, saved through `PUT /api/v1/events/{id}/budget`
  - a link to the ticket settings page from the event form
- **Registration counts.** `GET /api/v1/events/{id}/registrations` is for organizers. It counts bookings the same way FairSeat counts them for capacity (confirmed seats plus unexpired holds), so nothing is counted twice. FairSeat's existing locks still refuse over-capacity bookings, and a test races two bookings for the last seat to check this.
- **Low and sold-out warnings.**
  - **Low** means seats left ≤ max(10% of capacity, 5) (`LOW_TICKET_PERCENT`, `LOW_TICKET_SEATS`).
  - When full, the booking page shows **SOLD OUT**, or **REGISTRATION CLOSED** once the booking window has ended.
- **Budget.** Amounts come from TrustLedger by `event_id` and are shown separately:
  - **Spent:** paid expenses and reimbursements.
  - **Approved, not paid:** approved claims that haven't been paid out yet.
  - **Pending:** claims still awaiting approval.

  Going over budget shows a warning and blocks nothing.

## Configuration

See the Batch A section of `.env.example`. Defaults: payments mock, payouts mock, reminders hourly.

## 4. Volunteer duplicate and clash review (Batch B)

Migration `pending/043_member2_crew_shifts.sql` adds an optional shift (start and end time) to a task, in a new table. The `crew_tasks` table itself is unchanged.

Workflow:
1. The organizer adds a task, or changes who owns one.
2. The page calls `POST /api/v1/events/{id}/tasks/review`.
3. If something is flagged, a dialog lists the issues and suggests up to three people who are free at that time.
4. The organizer then picks someone else, cancels, or chooses **Assign anyway** with a reason of at least 5 characters.

What gets flagged, checked against the person's unfinished tasks in open events:

| Code | Meaning | Can override |
|---|---|---|
| `exact_duplicate` | same task title, same event | **never** |
| `near_duplicate` | nearly the same title in the same event ("Book DJ" / "Book the DJ") | yes |
| `overlapping_shift` | shifts overlap within one event | yes |
| `event_clash` | a duty in another event at an overlapping time (a task's time is its shift, or else its event's time) | yes |
| `over_limit` | more than `CREW_MAX_OPEN_TASKS` unfinished tasks (0 = no limit) | yes |

- **Checks are repeated on save.** Saving runs the same checks again on the server, under a per-person database lock, so two organizers assigning the same volunteer at once can't both get through.
- **Overrides are recorded.** Each override goes into `audit_logs` (`crewpulse.assignment_override`) and the task's activity trail, with the reason.
- **Nothing is removed or moved.** No task is ever deleted or reassigned automatically.

## Stripe test mode (verified)

- **Tested end to end:** with `PAYMENT_PROVIDER=stripe` and an `sk_test_` key, a membership dues payment was completed on Stripe's hosted checkout with test card 4242 4242 4242 4242. On return, the app checked the session with Stripe, activated the membership and posted one ₹500.00 income entry to the ledger.
- **Use the same address as `APP_BASE_URL`.** Stripe sends the payer back to `APP_BASE_URL`. Open the app at that exact address (for example `http://localhost:8000`), or the sign-in cookie won't carry over.
- **Webhooks need the Stripe CLI.** For webhooks as well as the return page, run `make stripe-listen` and put the printed `whsec_` in `STRIPE_WEBHOOK_SECRET`.
- **Stripe can't send money to UPI IDs.** Reimbursement payouts therefore use `PAYOUTS_PROVIDER=mock` (no money moves) or RazorpayX.
