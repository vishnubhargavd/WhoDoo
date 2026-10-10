# WhoDoo — FairSeat ticket booking (Phase 3A)

FairSeat lets a club sell a limited number of tickets to an event without ever selling the same seat twice. This phase adapts Ameer's Phase 3A package into the shared FastAPI backend. It holds seats and confirms free tickets. **It takes no payment, writes nothing to the ledger and issues no QR codes.**

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
| Tests | `tests/test_fairseat.py`, `tests/test_fairseat_integration.py`, `tests/test_event_tickets.py`, `tests/test_fairseat_pricing.py` |

## Running it

```bash
make test-fairseat      # 69 tests against the local whodoo_test database
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
| Cancel, any paid booking | Refused: `409 cancellation_requires_refunds`, nothing released |
| Complete | Always allowed |

With no seats booked, all of these are allowed. With `FAIRSEAT_ENABLED=0` the guards do nothing and no ticket table is touched.

### For Ameer's FairSeat frontend

All under `/api/v1`, signed-in members of the event's club only, `404 fairseat_disabled` when switched off.

1. **Find events:** `GET /discovery/events`, `GET /discovery/events/{event_id}`. Each event has `sale_state` (`not_open`, `open`, `closed`), `booking_opens_at`, `booking_closes_at`, `availability: "live"`, and visible `categories` with `id`, `name`, `description`, `price_paise`, `currency`, `member_discount_eligible`, `capacity`, `max_per_booking`, `seats_remaining`, and the category's own `sale_state`, `booking_opens_at`, `booking_closes_at`.
2. **Price:** `POST /ticket-bookings/quotes` with `event_id`, `ticket_category_id`, `quantity`. Returns the quote `id`, `unit_price_paise`, `total_price_paise`, discount fields and `expires_at` (5 minutes).
3. **Reserve:** `POST /ticket-bookings` with the same three fields plus `idempotency_key` and `accepted_quote_id`. `201` new, `200` replay.
4. **Read and cancel:** `GET /ticket-bookings`, `GET /ticket-bookings/{id}`, `POST /ticket-bookings/{id}/cancel`.

`price_paise` in discovery is the standard price. What a member pays comes only from the quote. `seats_remaining` is a snapshot; the reservation is what decides.

Organizer routes (president or the event's lead): `GET` and `PUT /events/{event_id}/tickets` for the whole configuration, as documented in `README_MEMBER2.md`.

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

Two files, both in `migrations/pending/`, applied in this order:

| File | Tables |
|---|---|
| `020_fairseat.sql` | `ticket_categories`, `ticket_quotes`, `ticket_bookings`, `ticket_booking_commands`, `event_tickets` |
| `021_fairseat_event_settings.sql` | `event_ticket_settings`; new columns and constraints on `ticket_categories`; unique index `events (id, club_id)`; the partition triggers |

They sit in `pending/` on purpose. The migration runner reads only `migrations/*.sql`, so `make migrate`, `make seed` and `make run` do not apply them. `tests/conftest.py` applies them to the local `whodoo_test` database only, after the real migrations. To adopt them: `git mv migrations/pending/02*.sql migrations/` and run `make migrate`.

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

- The only page is the organizer's ticket settings (`/events/{id}/tickets`). Buying is API only, and Ameer's `web/` app is not connected to it.
- No scheduled job runs `expire_holds`. Correctness does not depend on it, because expired holds are ignored by the count and tidied on the next booking.
- A purchaser must belong to the event's club, because a user has exactly one club today.
- Not run against Supabase.
