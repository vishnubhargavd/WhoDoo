# WhoDoo — FairSeat ticket booking and payments

FairSeat lets a club sell a limited number of tickets to an event without ever selling the same seat twice. This phase adapts Ameer's Phase 3A package into the shared FastAPI backend. It holds seats and confirms free tickets. Paying for a held booking, the ledger entries that go with it, and refunds are a second, separately switched part (see "Payments"). **It issues no QR codes; that is GateSync's job.** No real payment provider has been contacted: tests use the mock provider and stub Stripe's network calls.

It is switched off by default and its tables are a pending migration, so a normal `make run` is unaffected.

| Piece | File |
|---|---|
| Routes | `app/api/v1/fairseat.py` |
| Booking rules, locks, expiry | `app/services/fairseat_service.py` |
| Price calculator and quotes | `app/services/fairseat_pricing.py` |
| Tables | `app/models/fairseat.py`, `app/models/tickets.py`, `migrations/pending/020_fairseat.sql`, `021_fairseat_event_settings.sql` |
| Event ticket settings, discovery, lifecycle guards (Member 2) | `app/services/ticket_service.py`, `ticket_rules.py`, `app/api/v1/event_tickets.py` |
| Lock order | `app/services/ticket_locks.py` |
| Seat counting | `app/services/ticket_inventory.py` |
| Request and response shapes | `app/schemas/fairseat.py` |
| Payment routes | `app/api/v1/fairseat_payments.py` |
| Payment, late payment and refund rules | `app/services/fairseat_payments.py` |
| Tests | `tests/test_fairseat.py`, `tests/test_fairseat_integration.py`, `tests/test_event_tickets.py`, `tests/test_fairseat_payments.py`, `tests/test_fairseat_pricing.py` |

## Running it

```bash
make test-fairseat      # 96 tests against the local whodoo_test database
make fairseat-vectors   # regenerate price test vectors from the TypeScript calculator
```

To try the routes by hand you would need `FAIRSEAT_ENABLED=1` and the migration applied to your database. Neither is done for you: see "Migration" below.

## Event side and reservation side together

Member 2's event-side ticket configuration and this reservation engine are one system on this branch.

**Tables.** One category table, `ticket_categories`. One settings row per ticketed event, `event_ticket_settings` (Member 2's, with the per-booking limit capped at 6). His `event_ticket_categories` is not created; `ticket_categories` gained his fields instead.

| Member 2's field | Stored as |
|---|---|
| `price_paise` | `ticket_categories.base_price_paise` |
| `is_visible` | `ticket_categories.active` |
| `member_discount_eligible`, `description`, `sort_order` | new columns of the same names |

**Migrations, in order, all pending:** `020_fairseat.sql`, then `021_fairseat_event_settings.sql`. `021` replaces Member 2's proposed `012_event_tickets.sql`. It adds the settings table, the category columns, a foreign key from categories to settings, and his deferred trigger that enforces the partition.

**Rules that join the two sides**

- **Strict partition.** While sales are on, the categories' seats add up to the event's capacity exactly. Booking checks both the category's seats and the event's total.
- **Booking needs** a published event with a start time, a settings row with `sales_enabled`, a visible category, and a moment inside the event's booking window and the category's own window if it has one. A category window can only narrow the event's.
- **At most 6 tickets per booking**, in the organizer input, the booking API and the database. An event may set a smaller `max_per_booking`. The limit of 6 per person per event still applies.
- **Category names are 1 to 120 characters; a ticket costs at most 10,000,000 paise.** Both are checked in the API and the database.
- **One category writer.** `ticket_service.save_config` is the only code that writes categories. `PUT /events/{id}/tickets`, the organizer page, and `POST`/`PATCH /ticket-categories` all end there, so the same checks run whichever is used.
- **Seat counts are real.** `ticket_inventory` is the single definition (confirmed seats plus unexpired holds). The event side's locks, the settings page and discovery all read it.
- **Not eligible for member discount** means the category is priced as if no membership were presented. The calculator is unchanged.

**Lock order:** event row, settings row, category rows in id order, booking rows. Booking, cancelling, expiring holds, saving settings and changing an event's status or times all take locks through `ticket_locks`. A test holds each row in turn from another connection and checks where each operation stops and what it already holds.

**Event lifecycle** (in `event_service.change_status` and `update_event`, through `ticket_service` guards, inside the same transaction):

| Change | With seats booked |
|---|---|
| Back to draft | Refused: `409 ticket_sales_started` |
| Reschedule | Refused: `409 ticket_sales_started` |
| Cancel, only unpaid holds and free bookings | Allowed. Holds are cancelled and free tickets revoked in the same transaction |
| Cancel, any paid booking | With ticket payments on: allowed. The bookings are cancelled and their tickets revoked in the same transaction, then refunded after it commits. With ticket payments off: refused, `409 cancellation_requires_refunds`, nothing released |
| Complete | Always allowed |

With no seats booked, all of these are allowed. With `FAIRSEAT_ENABLED=0` the guards do nothing and no ticket table is touched.

### For Ameer's FairSeat frontend

All under `/api/v1`, signed-in members of the event's club only, `404 fairseat_disabled` when switched off.

1. **Find events:** `GET /discovery/events`, `GET /discovery/events/{event_id}`. Each event has `sale_state` (`not_open`, `open`, `closed`), `booking_opens_at`, `booking_closes_at`, `availability: "live"`, and visible `categories` with `id`, `name`, `description`, `price_paise`, `currency`, `member_discount_eligible`, `capacity`, `max_per_booking`, `seats_remaining`, and the category's own `sale_state`, `booking_opens_at`, `booking_closes_at`.
2. **Price:** `POST /ticket-bookings/quotes` with `event_id`, `ticket_category_id`, `quantity`. Returns the quote `id`, `unit_price_paise`, `total_price_paise`, discount fields and `expires_at` (5 minutes).
3. **Reserve:** `POST /ticket-bookings` with the same three fields plus `idempotency_key` and `accepted_quote_id`. `201` new, `200` replay.
4. **Read and cancel:** `GET /ticket-bookings`, `GET /ticket-bookings/{id}`, `POST /ticket-bookings/{id}/cancel`.

`price_paise` in discovery is the standard price. What a member pays comes only from the quote. `seats_remaining` is a snapshot; the reservation is what decides.

Organizer routes (president or the event's lead): `GET` and `PUT /events/{event_id}/tickets` for the whole configuration, as documented in `MEMBER2_EVENT_OPERATIONS.md`.

## API

All under `/api/v1`. Every route answers `404 fairseat_disabled` unless `FAIRSEAT_ENABLED=1`. Errors use the app's usual `{"error": {"code", "message", "details"}}` shape.

| Method | Path | Who | Notes |
|---|---|---|---|
| GET | `/ticket-categories?event_id=` | Anyone in the event's club | Capacity, seats taken, seats left, whether it is on sale |
| POST | `/ticket-categories` | President, or the event's lead | One more category for an event that already has ticket settings. Same writer and rules as `PUT /events/{id}/tickets` |
| PATCH | `/ticket-categories/{id}` | President, or the event's lead | Capacity cannot go below seats taken. A price change invalidates old quotes |
| POST | `/ticket-bookings/quotes` | Anyone in the event's club | The server's price for you, valid 5 minutes. Holds no seats |
| POST | `/ticket-bookings` | Anyone in the event's club | Body: event, category, quantity, `idempotency_key`, `accepted_quote_id`. `201` new, `200` replay |
| GET | `/ticket-bookings`, `/ticket-bookings/{id}` | Purchaser; event lead and president for their event | |
| POST | `/ticket-bookings/{id}/cancel` | Purchaser | Unpaid holds and free bookings only |

A request cannot name a user, club, role, price or status. Unknown fields are rejected with `422`.

## Rules

- **Capacity is per ticket category, under the event's total.** Seats in use are confirmed seats plus seats on a hold that has not run out.
- **A hold lasts 10 minutes.** It stops counting the instant its deadline passes, whether or not anything has marked it expired.
- **At most 6 tickets per booking, and 6 per person per event** across all categories, counted inside the same lock as capacity.
- **Sales window:** the opening instant is included and the closing instant excluded. The event's booking window bounds every category; a category may have a narrower one. Sales cannot close later than the event starts, and nothing can be booked once it has started.
- **Only published events with a start time and ticket sales switched on can be booked.** Otherwise `409 event_not_bookable` or `409 sales_not_enabled`.
- **Free tickets** (total of zero) are confirmed at once with `payment_status = not_required` and get one `event_tickets` row per seat, with no credential.
- **Paid tickets stay at `pending_payment`** until the payment provider confirms payment (see "Payments").
- **Cancelling** gives the seats back at once and revokes the ticket rows. A paid booking can be cancelled by its purchaser until the event starts, and is refunded in full. With ticket payments switched off, cancelling a paid booking is refused with `refund_required`.
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

Three files, all in `migrations/pending/`, applied in this order:

| File | Tables |
|---|---|
| `020_fairseat.sql` | `ticket_categories`, `ticket_quotes`, `ticket_bookings`, `ticket_booking_commands`, `event_tickets` |
| `021_fairseat_event_settings.sql` | `event_ticket_settings`; new columns and constraints on `ticket_categories`; unique index `events (id, club_id)`; the partition triggers |
| `022_fairseat_payments.sql` | `ticket_payment_events`; payment and refund columns and a stricter state check on `ticket_bookings` |

They sit in `pending/` on purpose. The migration runner reads only `migrations/*.sql`, so `make migrate`, `make seed` and `make run` do not apply them. `tests/conftest.py` applies them to the local `whodoo_test` database only, after the real migrations. To adopt them: `git mv migrations/pending/02*.sql migrations/` and run `make migrate`.

## Payments

Needs `FAIRSEAT_PAYMENTS_ENABLED=1` as well as `FAIRSEAT_ENABLED=1`, and uses whichever provider `PAYMENT_PROVIDER` selects (Stripe test mode or the mock). These routes are separate from the merchandise routes and never touch a merchandise order.

| Method | Path | Who | Notes |
|---|---|---|---|
| POST | `/ticket-payments/stripe/create-checkout-session` | Purchaser | Body: `booking_id` only. For a booking in `pending_payment` whose hold has not run out |
| POST | `/ticket-payments/stripe/verify` | Purchaser | Body: `session_id`. Asks the provider, then confirms or starts a refund |
| GET | `/ticket-payments/stripe/return?session_id=` | Purchaser | Where the provider sends the buyer back; same as verify |
| POST | `/ticket-payments/stripe/webhook` | Provider | `Stripe-Signature` checked. Handles `checkout.session.completed`, `payment_intent.succeeded`, `payment_intent.payment_failed`, `refund.updated`, `refund.failed`, `charge.refund.updated`, `charge.refunded` |
| POST | `/ticket-payments/mock/simulate` | Purchaser | Mock provider only |
| GET | `/ticket-payments/refunds` | Treasurer, president | Refunds owed, in flight or failed |
| POST | `/ticket-payments/refunds/{booking_id}/retry` | Treasurer, president | Send a failed or stuck refund again |

Verify and return answer with `outcome`: `confirmed`, `refund_required`, `refund_pending`, `refunded` or `refund_failed`.

**What is trusted.** The amount, currency and owner come from the booking row, which was priced from a server quote. Whether a payment happened comes from the provider: a session the server fetches itself, or a signed webhook. A request body carries only a booking id or a session id.

**Confirming.** A confirmed payment is recorded under the event, settings, category and booking locks. The booking becomes `confirmed`/`paid`, one `event_tickets` row per seat is created, and one ledger entry is posted, all in one transaction. Reporting the same payment again, by any route and any number of times, changes nothing.

**The provider is never called while locks are held.** Each step either talks to the provider or holds locks. A test proves it by trying to lock the same rows from another connection during every provider call.

**Paying late.** A hold lasts 10 minutes, but Stripe's shortest checkout session is 30, so a buyer can pay after the hold has gone. Then, under the locks:

- if the seats are still free in the category and within the event's total, the event is still published, selling and not started, and the purchaser is within their limit, the booking is confirmed;
- otherwise the payment is recorded, the booking gets no seats (`expired`, or `cancelled` if it had been cancelled), and a full refund is owed, with the reason stored (`seats_reassigned`, `event_not_bookable`, `purchaser_limit`, `booking_cancelled`).

Seats that went to someone else are never taken back.

**Refunds.** A refund is three steps: mark it and commit; ask the provider; record the answer.

| `refund_status` | `payment_status` | Meaning |
|---|---|---|
| `required` | `paid` | Owed, not yet sent |
| `pending` | `paid` | Sent; the provider has not confirmed it |
| `succeeded` | `refunded` | The provider confirmed it. Only now is the ledger refund entry written |
| `failed` | `paid` | The provider refused or was unreachable; the reason is stored and a treasurer can retry |

A booking is never shown as refunded before the provider says so. A second attempt after a failure uses a new provider idempotency key; repeating an attempt that is still pending reuses its key, so the money cannot go back twice. A full refund made in the Stripe Dashboard is picked up from the webhook; partial refunds are not recorded.

**A declined card** sets `payment_status = failed` and leaves the hold, so the buyer can try again until it runs out. Nothing is issued.

**A wrong amount or currency** is not accepted: nothing changes and the mismatch is written to the audit log. A second, different payment for one booking is flagged as `duplicate_payment` in the audit log for the treasurer and not applied.

## Ledger

| Event | Entry |
|---|---|
| Payment recorded | `record_income`, `source_module="event_ticket"`, `category="ticket_sales"`, `event_id`, `reference_type="ticket_booking"`, `reference_id` = booking id, key `<payment_intent_id>:income` |
| Refund confirmed by the provider | `record_refund`, same references, key `<payment_intent_id>:refund` |

The amount is `Decimal(total_price_paise) / Decimal(100)`, never a float. Income is posted whenever money arrives, including a late payment that is then refunded, so the ledger agrees with the provider. Entries are never edited or deleted. Merchandise accounting is unchanged.

**Tickets sold.** The event profit and loss used to count ledger income entries, which counts a booking of five seats as one ticket. With FairSeat on it now counts seats in paid, confirmed bookings, plus one for each ticket income entry that is not tied to a booking. With FairSeat off it is unchanged.

## GateSync handoff

`event_tickets` is the handoff. FairSeat creates one row per seat when a booking is confirmed, and GateSync does the rest. FairSeat generates no credential and has no scanner logic.

| Column | Meaning for GateSync |
|---|---|
| `id` | Ticket identifier. An internal reference only; it must not be what a QR code proves |
| `booking_id` | Join to `ticket_bookings` for `ticket_category_id`, `status` and `payment_status` |
| `event_id` | The event the ticket admits to |
| `holder_user_id` | The authorised attendee (today, the purchaser for every seat of the booking) |
| `seat_number` | 1..quantity within the booking |
| `status` | `pending_issue` → `issued` → `checked_in`, or `revoked` from any state |
| `credential_digest` | NULL until GateSync issues. 32 bytes, unique |
| `issued_at`, `checked_in_at`, `revoked_at` | Set with the matching status |

What GateSync must do:

- **Issue** only for rows with `status = 'pending_issue'` whose booking is `confirmed`. Generate a random secret of at least 128 bits, give it to the attendee in the QR code, and store only its SHA-256 digest in `credential_digest`. Never store the secret, and never put a ticket id alone in the QR code: ids are not secret.
- **Issue idempotently:** `UPDATE event_tickets SET credential_digest = :d, status = 'issued', issued_at = now() WHERE id = :id AND status = 'pending_issue'`. Zero rows updated means it was already issued or revoked.
- **Check in atomically:** hash the scanned secret, then `UPDATE event_tickets SET status = 'checked_in', checked_in_at = now() WHERE credential_digest = :d AND event_id = :event AND status = 'issued' RETURNING id`. One row means admit. Zero rows means refuse, and a second scan of the same code gets zero rows.
- **Respect revocation.** FairSeat sets `revoked` when a booking is cancelled or refunded. A revoked ticket never matches the check-in update. GateSync should not un-revoke.
- **Leave `checked_in` alone.** FairSeat refuses a purchaser's cancellation once any ticket of the booking is checked in.

Still open for the team: naming attendees other than the purchaser, and reissuing a lost credential.

## Known gaps

- Stripe has not been contacted for tickets. The Stripe path is tested with its network calls stubbed.
- A refund that fails needs a treasurer to retry it; nothing retries automatically.
- If the server stops between marking a refund pending and hearing from the provider, it stays `pending` until the webhook arrives or a treasurer retries. Retrying is safe.
- The only page is the organizer's ticket settings (`/events/{id}/tickets`). Buying is API only, and Ameer's `web/` app is not connected to it.
- No scheduled job runs `expire_holds`. Correctness does not depend on it, because expired holds are ignored by the count and tidied on the next booking.
- A purchaser must belong to the event's club, because a user has exactly one club today.
- Not run against Supabase.
