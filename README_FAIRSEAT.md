# WhoDoo — FairSeat ticket booking (Phase 3A)

FairSeat lets a club sell a limited number of tickets to an event without ever selling the same seat twice. This phase adapts Ameer's Phase 3A package into the shared FastAPI backend. It holds seats and confirms free tickets. **It takes no payment, writes nothing to the ledger and issues no QR codes.**

It is switched off by default and its tables are a pending migration, so a normal `make run` is unaffected.

| Piece | File |
|---|---|
| Routes | `app/api/v1/fairseat.py` |
| Booking rules, locks, expiry | `app/services/fairseat_service.py` |
| Price calculator and quotes | `app/services/fairseat_pricing.py` |
| Tables | `app/models/fairseat.py`, `migrations/pending/020_fairseat.sql` |
| Request and response shapes | `app/schemas/fairseat.py` |
| Tests | `tests/test_fairseat.py`, `tests/test_fairseat_pricing.py` |

## Running it

```bash
make test-fairseat      # 32 tests against the local whodoo_test database
make fairseat-vectors   # regenerate price test vectors from the TypeScript calculator
```

To try the routes by hand you would need `FAIRSEAT_ENABLED=1` and the migration applied to your database. Neither is done for you: see "Migration" below.

## API

All under `/api/v1`. Every route answers `404 fairseat_disabled` unless `FAIRSEAT_ENABLED=1`. Errors use the app's usual `{"error": {"code", "message", "details"}}` shape.

| Method | Path | Who | Notes |
|---|---|---|---|
| GET | `/ticket-categories?event_id=` | Anyone in the event's club | Capacity, seats taken, seats left, whether it is on sale |
| POST | `/ticket-categories` | President, or the event's lead | Name, `base_price_paise`, capacity, sales window |
| PATCH | `/ticket-categories/{id}` | President, or the event's lead | Capacity cannot go below seats taken. A price change invalidates old quotes |
| POST | `/ticket-bookings/quotes` | Anyone in the event's club | The server's price for you, valid 5 minutes. Holds no seats |
| POST | `/ticket-bookings` | Anyone in the event's club | Body: event, category, quantity, `idempotency_key`, `accepted_quote_id`. `201` new, `200` replay |
| GET | `/ticket-bookings`, `/ticket-bookings/{id}` | Purchaser; event lead and president for their event | |
| POST | `/ticket-bookings/{id}/cancel` | Purchaser | Unpaid holds and free bookings only |

A request cannot name a user, club, role, price or status. Unknown fields are rejected with `422`.

## Rules

- **Capacity is per ticket category.** Seats in use are confirmed seats plus seats on a hold that has not run out.
- **A hold lasts 10 minutes.** It stops counting the instant its deadline passes, whether or not anything has marked it expired.
- **At most 6 tickets per booking, and 6 per person per event** across all categories, counted inside the same lock as capacity.
- **Sales window:** `sales_open_at` is included, `sales_close_at` is excluded, and sales cannot close later than the event starts. Nothing can be booked once the event has started.
- **Only published events with a start time can be booked.** Anything else gets `409 event_not_bookable`.
- **Free tickets** (total of zero) are confirmed at once with `payment_status = not_required` and get one `event_tickets` row per seat, with no credential.
- **Paid tickets stop at `pending_payment`.** Nothing in this phase can mark a booking paid.
- **Cancelling** an unpaid hold or a free booking gives the seats back and revokes its ticket rows. A paid booking is refused with `refund_required`.
- **Waitlist: not offered.** A full category answers `409 sold_out` with `"waitlist": "unavailable"`. There is no waitlist table, so no entry can be left waiting forever.

## How overselling is prevented

Anything that changes the seats in use (booking, cancelling, expiring holds, editing a category) first locks the event row, then the category rows in id order, with `SELECT … FOR UPDATE`. The seat count is taken while holding those locks, so one event's bookings happen one at a time. The locks last until the route commits.

Services flush and never commit, like the rest of the app. The route commits once, so a booking, its idempotency record and its ticket rows are stored together or not at all.

**Idempotency.** `ticket_booking_commands` has a unique key on purchaser, event and idempotency key, with a fingerprint of the request. The same key and request returns the original booking. The same key with a different category, quantity or quote is `409 idempotency_conflict`.

## Pricing

`calculate_ticket_discount` is a port of MemberCore's TypeScript calculator (`web/src/modules/membercore/ticket-discount.ts`): whole paise, integer arithmetic, halves rounded up. The two are tied together by shared test vectors. `make fairseat-vectors` runs the TypeScript original over 379 inputs, and the Python tests must match every one. A third test regenerates the vectors from the TypeScript source and fails if they have changed.

**Member discounts are not active.** The backend has no membership records, only `users.is_active_member`, which has no discount rate or dates. So the default membership source reports no membership and everyone is quoted the base price. The discount path is tested end to end with a stand-in source. To switch it on, the team must approve where memberships live and call `fairseat_pricing.set_membership_source(...)`.

A quote is stored server-side and bound to the purchaser, event, category, quantity, calculator version and the category's price version. At booking time the price is worked out again; if anything differs the booking is refused with `quote_stale`.

## Sign-in

The purchaser is whoever `get_current_user` returns. Today that is the demo picker, where anyone can act as anyone, so:

- booking is off by default, and
- the app refuses to start with `FAIRSEAT_ENABLED=1` and `DEMO_AUTH=1` on a database that is not local.

When the Supabase Auth bridge is approved, only `get_purchaser` in `app/api/v1/fairseat.py` changes. It must keep returning a row from `users`. JWT verification has not been touched.

## Migration

`migrations/pending/020_fairseat.sql` adds five tables: `ticket_categories`, `ticket_quotes`, `ticket_bookings`, `ticket_booking_commands`, `event_tickets`. It alters nothing that exists; it only references `events.id` and `users.id`.

It sits in `pending/` on purpose. The migration runner reads only `migrations/*.sql`, so `make migrate`, `make seed` and the tests' reset do not apply it. `tests/conftest.py` applies it to the local `whodoo_test` database only. To adopt it: `git mv migrations/pending/020_fairseat.sql migrations/` and run `make migrate`.

## What Member 2 needs to agree

Nothing in the event module was changed. Three things need a decision:

1. **Cancelling an event that has bookings.** `change_status` knows nothing about tickets. The proposed fix is one call inside it, in the same transaction, when an event becomes `cancelled`: `await fairseat_service.release_bookings_for_event(db, event.id)`. It gives back every hold and free booking and revokes their tickets. If any booking has been paid for it raises `409 paid_bookings_exist`, which blocks the cancellation until refunds exist. The function is written and tested but not called from anywhere. Until it is wired in, a cancelled event simply stops taking bookings and its free bookings stay marked confirmed.
2. **Moving an event's start time** earlier than a category's `sales_close_at`. Booking is still refused after the start, so nothing can be oversold, but `update_event` does not adjust the window.
3. **Event-wide capacity.** `events` has no capacity column. If one is added, the check belongs in `reserve` next to the category check; the event row is already locked there.

## Connecting payment later (not built)

Payments are now Stripe, not Razorpay. Ticket payment must not reuse the merchandise routes, which are tied to `merch_orders`.

- **Session.** A ticket route opens a Stripe Checkout Session for a booking in `pending_payment`, with the amount read from `ticket_bookings.total_price_paise` and `ticket_booking_id` in the metadata. The session should expire no later than the hold.
- **Verification.** As for merchandise: retrieve the session with the secret key, check it is paid, belongs to the purchaser and matches the booking's amount and currency. The webhook does the same settlement. `payment_events.provider_event_id` already makes repeated deliveries harmless and can be reused.
- **Confirming.** Under the event and category locks: if the hold is still live, set `confirmed`/`paid` and create the ticket rows. If the hold has expired, recheck capacity; confirm if the seats are still free, otherwise refund in full. This is the same shape as merchandise's paid-but-sold-out path.
- **Ledger.** One `record_income(source_module="event_ticket", amount=Decimal(total_price_paise) / 100, event_id=…, reference_id=booking.id, reference_type="ticket_booking", idempotency_key=f"{payment_intent_id}:income")`, in the same transaction as the confirmation.
- **Refunds.** `record_refund` with key `f"{payment_intent_id}:refund"`, the Stripe refund against the PaymentIntent, booking to `cancelled`/`refunded`, tickets revoked.
- **Several tickets in one booking** are one payment and one ledger entry for the total. "Tickets sold" in finance reports should then count `event_tickets` rows, not ledger entries, which is what it does today.
- **GateSync.** `event_tickets` is the handoff: one row per seat, `pending_issue`, no credential. GateSync fills `credential_digest` and sets `issued`; check-in is a conditional update to `checked_in`; cancellation sets `revoked`.

## Known gaps

- No pages; this phase is API only, and Ameer's `web/` app is not connected to it.
- No scheduled job runs `expire_holds`. Correctness does not depend on it, because expired holds are ignored by the count and tidied on the next booking.
- A purchaser must belong to the event's club, because a user has exactly one club today.
- Not run against Supabase.
