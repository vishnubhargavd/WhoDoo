# FairSeat Phase 2A — integration preparation

## Current implementation

`booking-rules.ts` is a pure, single-seat decision function over a trusted event snapshot and the existing MemberCore `DiscountResult`. It does not persist bookings, allocate seats, authenticate a purchaser, or establish a concurrency guarantee. This phase adds local request/transaction contracts and deterministic validation only. It adds no database adapter, route, migration, payment integration, UI, or fake in-memory persistence.

`booking-contracts.ts` distinguishes browser booking selectors from server-trusted identity, event, capacity, and price data. IDs are FairSeat-local TypeScript brands, not shared schema declarations. Supabase Auth identity and the existing application user ID are distinct types. The membership price snapshot is the MemberCore result without recalculation. The request parser rejects client-supplied identity, membership, capacity, price, and lifecycle fields. Quote comparison is a consistency helper over server-loaded quotes; it is not a signature or authorization check.

## Proposed lifecycle

1. The server verifies the Supabase session with the supported server-side Auth API. It resolves the existing application user ID through a team-approved identity mapping; a Supabase Auth UUID is never used as a `users.id` by assumption.
2. It loads the event and ticket category from the event owner’s approved read contract, verifies club scope and booking window, and rejects invalid/cancelled events. The browser’s event state, category price, membership flag, capacity, or seat count is never authoritative.
3. It loads membership through MemberCore for that mapped application user and event club, then invokes the existing MemberCore calculator. FairSeat stores a price/discount snapshot from the returned `DiscountResult`; it does not duplicate the discount formula.
4. The client may display a short-lived quote. At reservation commit, the server re-reads/locks current event/category/price/membership state. It compares the purchaser’s accepted server quote with the current quote. A material price or eligibility change returns `price_changed` and requires renewed acceptance.
5. One PostgreSQL transaction claims the idempotency key, serializes capacity for the event/category, inserts either a pending-payment booking with a hold expiry or a waitlist entry, and returns the persisted result. Replays of the same key and same request return the original outcome; the same key with a different request is rejected.
6. Payment-provider callbacks are signature-verified before entering the database port and deduplicated by the provider event ID. A valid paid transition changes a live pending booking to confirmed once. Failed or late payment does not silently reclaim an expired reservation; the team must agree on refund/reconciliation behavior.
7. Expiry and cancellation change booking state and release held capacity in a transaction, then promote the oldest eligible waitlist entry under the same event/category lock. Promotion creates a new short hold and revalidates the current price/member eligibility; the attendee must accept the offer and complete payment within the agreed period.
8. GateSync receives an issuance event only after a confirmed booking and the agreed payment state (`paid`, or `not_required` for an approved free-event flow). Ticket issuance and booking cancellation/refund revocation need a transaction/outbox contract so no QR is issued for an unconfirmed booking.

## Concurrency and idempotency

All writers that consume or release capacity must use the same lock boundary: reservation, payment confirmation, expiry, cancellation, and waitlist promotion. A simple initial PostgreSQL strategy is to lock the event/category capacity row with `SELECT ... FOR UPDATE`, recompute occupied seats from confirmed bookings plus unexpired holds, validate the requested quantity, and insert/update the booking in that same transaction. Competing purchases for the last seat serialize; only the first transaction can observe and claim it. A conditional atomic counter update (`reserved = reserved + quantity WHERE reserved + quantity <= capacity`) is an alternative only if all lifecycle transitions maintain the counter transactionally. An in-memory lock or client-side count is not sufficient.

Use a unique idempotency constraint scoped to the mapped purchaser and event (or another team-approved stable scope), store a canonical request fingerprint, and distinguish an exact retry from a conflicting reuse. Acquire/resolve that key in the same transaction as capacity reservation. Payment notifications need a unique provider-event key and an idempotent state transition. These are proposals until the event and payment owners agree on the records and transaction boundary.

## Minimal database proposal (not approved schema)

- **Bookings:** booking ID; approved event FK; approved purchaser FK to the existing `users` identity; category reference when categories exist; quantity; integer-paise unit and total amount; MemberCore discount snapshot (eligibility/reason/basis points); explicit `pending_payment | confirmed | cancelled | expired` status; payment status; idempotency key plus canonical request fingerprint; hold expiry and timestamps. Do not use Supabase Auth UUID as the FK without an approved mapping.
- **Ticket categories:** add only if Member 2 has no equivalent. Proposed fields are event reference, label, integer-paise price, optional per-category capacity, sale window and active flag. Do not create duplicate event/category definitions.
- **Capacity:** derive occupied units from confirmed bookings and unexpired holds under a row lock for the first implementation. Add an atomic counter only if the team chooses and tests its transactional maintenance. Event-wide and per-category capacities must not disagree.
- **Waitlist:** separate entry with event/category, existing application-user reference, quantity, monotonic queue order, explicit state, offer/hold expiry and timestamps. Unique active-entry policy and multiple-quantity promotion order need approval.
- **Payments:** consume Member 3’s existing payment/ledger contract. If it lacks callback idempotency, propose a provider event receipt with a unique provider/event key and booking reference; do not create a parallel ledger.
- **Tickets/check-in:** use GateSync’s local digest lookup contract only as a proposal. A persisted ticket references an approved booking/event/club contract, stores only the opaque credential digest and lifecycle timestamps/state, and has a unique digest. Check-in should use one atomic conditional redemption operation.

Candidate constraints/indexes include unique scoped idempotency key, unique provider event ID, unique credential digest, indexes for `(event_id, category_id, status, hold_expires_at)`, waitlist order/state, and lookups by application user. Exact FK targets, table names, enum values, payment fields, category model and club denormalization require owner approval. If club ID is copied onto a booking, enforce its equality with the event’s club rather than trusting the copy.

## Security and shared-contract risks

- Supabase Auth verification authenticates a person, not their application profile, club membership, role, payment, or ticket. Identity mapping and role lookup must be server-side and approved.
- RLS can protect browser-facing reads and ordinary user-scoped operations. FastAPI’s privileged database connection bypasses RLS; FastAPI must repeat authentication, club authorization, state validation and idempotency checks itself.
- Member 2’s event model and Member 3’s payment contract are not present in this checkout. Do not add foreign keys or payment callbacks until those owners provide their actual identifiers and lifecycle semantics.
- The current FairSeat decision engine accepts one-seat snapshots. Quantity-aware allocation must happen inside the database transaction; do not infer that the pure engine prevents concurrent overselling.
- GateSync’s committed code checks eligibility and resolves a digest read-only. It does not issue credentials, redeem tickets, authorize scanners, or prevent simultaneous check-ins.
- A quote is a display/acceptance record, not proof of payment. The server must resolve quote identity, recompute current pricing and bind the accepted quote to the verified purchaser and request.

## Test matrix

The new tests currently cover request shape, unsafe client-supplied fields, integer quantity, server quote equality/change/expiry, and ticket issuance gating. Existing tests cover MemberCore discount behavior and FairSeat’s one-seat decision. The following should be implemented as **PostgreSQL integration tests** once the schema/adapter is approved; pure TypeScript tests cannot prove locking, uniqueness, or durable transitions.

| Case | Required test layer |
| --- | --- |
| Last seat available | PostgreSQL transaction |
| Two concurrent buyers for the final seat | PostgreSQL concurrency test; exactly one reservation |
| Sold-out event | PostgreSQL transaction; waitlist variant separately |
| Exact booking open/close boundaries | Pure rules plus transaction integration |
| Invalid/foreign ticket category | PostgreSQL authorization/integrity test |
| Invalid quantity and overflow | Pure parser plus transaction boundary |
| Club mismatch | Server authorization and RLS test |
| Membership discount eligibility | MemberCore service unit plus persisted quote integration |
| Paise rounding | Existing MemberCore unit tests; no duplicate math |
| Price changes between quote and purchase | Pure quote comparison plus transaction integration |
| Duplicate idempotency key, exact retry and changed payload | PostgreSQL uniqueness/concurrency test |
| Hold expiry and capacity release | PostgreSQL clock-controlled transaction |
| Cancellation and capacity release | PostgreSQL transaction |
| Waitlist insertion and ordering | PostgreSQL transaction |
| Waitlist promotion under concurrent release | PostgreSQL concurrency test |
| Payment failure | Verified callback integration and state transition |
| Payment success | Verified callback integration and confirmation |
| Duplicate payment callback | Provider-event uniqueness/idempotency integration |
| No QR issuance before confirmation | Outbox/consumer integration; pure gate is only a guard |
| No capacity overselling under concurrency | PostgreSQL stress/concurrency test |

## Decisions for team approval

1. Identity mapping from Supabase Auth UUID to the existing application `users.id`, including which component owns and verifies the mapping.
2. Member 2’s event/category IDs, club scope, capacity fields, booking windows, cancellation behavior and read/authorization API.
3. Member 3’s payment provider, payment state/ledger contract, signature verification, idempotency key, late-success/refund handling and who may mark a booking paid.
4. Whether capacity is event-wide, category-specific, or both; hold duration; max quantity; and how waitlist quantity/order/offers work.
5. Whether a confirmed free booking uses `not_required`, and the exact payment state that permits GateSync issuance.
6. Shared migration owner, table/FK names, RLS roles/policies and how FastAPI privileged access repeats authorization.
7. Cancellation/refund/ticket-revocation and outbox behavior across FairSeat, TrustLedger and GateSync.

## Message to Member 2 and Member 3

> We are preparing FairSeat booking persistence on branch Ameer without adding migrations yet. Please share the exact current contracts you own: **Member 2:** event and optional ticket-category IDs, club relationship, capacity and booking-window fields, cancellation state, and the supported server-side read/authorization path. **Member 3:** payment/ledger booking reference, payment status transitions, provider callback verification and event-ID idempotency contract, and late-success/refund behavior. Please also confirm the existing application-user FK target and the payment state that means GateSync may issue a ticket. We will align FairSeat to those contracts rather than duplicate tables or infer columns.

## Next steps

After approval, add a read-only adapter for the agreed event/category and identity contracts, then implement a PostgreSQL reservation transaction with scoped idempotency and a two-request final-seat integration test. Add payment transitions and waitlist promotion only after their owners approve the contracts. Keep GateSync issuance behind the confirmed/payment transition. No live reservations are implemented by this phase.
