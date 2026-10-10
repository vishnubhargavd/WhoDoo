# FairSeat Phase 2B — FastAPI booking integration boundary

## Status

This is an integration proposal, not a claim that ticket booking is connected. The local Ameer checkout does not contain Member 2's event implementation or Member 3's payment implementation. Existing event routes are under `/api/v1/events`; no ticket capacity, booking-window, category, reservation, or ticket-payment routes are confirmed. The new TypeScript client therefore requires an explicitly configured full reservation endpoint and fails with `not_configured` when it is absent. Endpoint paths and JSON response variants below are **PROPOSED** until their owners approve them.

The client boundary in `booking-api.ts` accepts only the existing parsed event/category selectors, quantity, idempotency key, and a server-issued quote reference. It omits browser identity, club, role, membership, price, payment, and availability claims. Its authorization callback must run only in a trusted server context and may return a bearer credential only after the team approves how FastAPI verifies it. Never place this client or credential in a browser bundle. This transport does not authenticate, authorize, calculate pricing, reserve capacity, or provide a concurrency guarantee.

## Confirmed shared contracts and ownership

- Existing `events` has UUID `id` and `club_id`, status `draft | published | cancelled | completed`, `starts_at`/`ends_at` timestamptz, and `lead_id` referencing existing `users.id`. Member 2 owns event management. No ticket capacity, sale window, ticket price, or categories are confirmed.
- Existing `users.id` is the application UUID; `users.club_id`, `role`, email, full name, and `is_active_member` exist. `auth_user_id` does not. Existing FastAPI token validation expects JWT `sub` to equal the application `users.id`; this does not by itself verify Supabase Auth tokens.
- Existing payment endpoints serve merchandise orders. Do not send ticket booking IDs to them. Member 3's finance ledger supports `record_income`/`record_refund`, `source_module="event_ticket"`, Decimal rupees, and idempotency. Current ticket reporting counts paid ledger entries rather than ticket units.

## Proposed server API (unimplemented)

Only after Member 2 and Member 3 approve the paths and response contract, a minimal first purchase flow could expose:

1. `POST /api/v1/ticket-bookings/quotes` — resolve published event/category and MemberCore eligibility server-side, returning a short-lived quote reference and integer-paise totals.
2. `POST /api/v1/ticket-bookings` — accept event/category IDs, quantity, idempotency key, and accepted quote reference; atomically create a hold or waitlist result. The TypeScript response union is a candidate shape, not an existing API guarantee.
3. `GET /api/v1/ticket-bookings/{booking_id}` — return only a booking authorized for the authenticated purchaser or approved club role.
4. Payment order creation and verification routes plus the provider webhook — separate ticket-specific contracts that call Member 3's provider verification and ledger service. These paths are **not** merchandise endpoints and are **not implemented**.

The first purchasable vertical slice should begin with Member 2 publishing an agreed category/capacity/sale-window contract and Member 3 confirming a ticket-specific payment flow. The existing UI/API boundary must remain disconnected until those contracts exist; it must never fake a reservation success.

## Proposed persistence and transaction rules (no migrations approved)

Reuse existing `events` and `users`; do not create a duplicate users, clubs, or events system. Subject to owner approval, add ticket-category configuration only if Member 2 confirms no equivalent exists. Proposed booking records reference the existing event and application user IDs, store category, quantity, a server-computed paise price snapshot and MemberCore discount explanation, lifecycle/payment state, a purchaser-scoped idempotency key plus request fingerprint, hold expiry, and timestamps. Proposed ticket rows represent each attendee credential separately and reference the approved booking/event/club contract. A proposed waitlist record stores event/category, application user, quantity, queue order, state, offer expiry, and timestamps. These are conceptual records only; no SQL names or columns are approved here.

For the first PostgreSQL adapter, serialize capacity writers on one agreed event/category capacity row with `SELECT ... FOR UPDATE`. In the same transaction, re-read published state, sale window, category, current price/membership quote and occupied units (confirmed tickets plus unexpired holds), claim the idempotency key, and insert a hold only if the requested quantity fits. Reservation, payment confirmation, hold expiry, cancellation, and waitlist promotion must use the same lock boundary. Store a canonical request fingerprint: same key and same request replays the original result; same key with a changed request fails. Pure TypeScript tests cannot demonstrate the concurrent final-seat guarantee.

Use either event-wide capacity or per-category capacity as an explicit Member 2 decision; do not allow the two totals to diverge. Agree on hold duration, maximum quantity, whether waitlist entries can request multiple tickets, queue order, promotion offer duration, and when an unaccepted offer expires. Waitlist promotion must recheck capacity and current price under the same lock and issue a fresh offer rather than silently carrying forward a stale quote.

## Payment, accounting, and GateSync handoff

Create the payment order only for a persisted booking using Member 3's approved ticket-specific provider contract. Verify provider signatures server-side. Verify booking ownership, booking reference, amount, currency, provider payment/order IDs, and the allowed state transition against database values; never accept a client claim that payment succeeded. Convert paise to Decimal rupees exactly as `Decimal(total_paise) / Decimal(100)` at the ledger boundary and require the approved INR currency. Do not use floating point.

Deduplicate callbacks with a unique provider event ID and apply the payment state, booking confirmation, ledger posting and downstream event/outbox record in a transaction or an approved idempotent outbox workflow. For each duplicate, the database must produce no second financial or ticket effect. A late success after a hold expired needs an approved recovery policy: either reacquire capacity under lock and confirm, or refund/reconcile through the idempotent `record_refund`; never create tickets for an expired/unreserved booking by default.

Current finance reporting counts paid ledger entries, which is not a reliable ticket quantity. The finance owners should report quantity from ticket/booking rows (or an agreed immutable quantity aggregate) while using the ledger for monetary totals. Decide how partial refunds, multiple categories, and cancelled attendance affect reports.

Issue GateSync credentials only after verified `paid` confirmation, or after Member 3 and the team explicitly approve the `not_required` free-booking path. Persist one ticket record per attendee, store only the credential digest, and use a unique constraint on it. A transactional outbox or equivalent idempotent handoff should connect payment confirmation to issuance. Cancellation/refund must revoke the associated ticket credentials; scan redemption must be one atomic conditional update so concurrent scans cannot both check in.

## Event lifecycle, club authorization, and RLS

Only `published` events may accept quotes or reservations. A transition back to `draft` must stop new sales and preserve existing holds/bookings; decide whether the transition is blocked while active sales exist or uses a coordinated pause. `cancelled` should stop new reservations and initiate the approved attendee notification/refund/credential-revocation workflow. `completed` should reject new bookings and follows the team's check-in/reporting retention policy. These transitions belong to Member 2's event contract and must not be implemented by FairSeat through undocumented event mutations.

The server resolves `events.club_id` and the user's club/role from trusted records. A purchaser may create/read only their own booking; an event lead/club officer may manage only events and ticket configuration within an explicitly approved club role matrix. Existing roles alone do not reveal which role can configure capacity, cancel events, issue refunds, or check in guests; obtain that matrix from the team. Never accept `club_id`, role, application user ID, membership state, or payment state from the browser as authorization.

RLS is defense in depth for direct Supabase access if that access is later approved. A privileged FastAPI database connection may bypass RLS, so every FastAPI endpoint still needs independent JWT verification and club/purchaser authorization in application code. Prefer forcing RLS on any future browser-accessible booking tables, deny anonymous writes, and scope purchaser reads by the mapped application identity. Policies must not treat user-editable JWT metadata as a role source. Exact policies depend on the chosen connection role and identity mapping and require coordinated review; no policy or migration is included in this phase.

## Supabase authentication and identity mapping

Supabase Auth UUIDs and application `users.id` UUIDs are separate identity namespaces. Propose a nullable unique `users.auth_user_id` FK/reference to `auth.users.id`, filled only by a trusted provisioning/linking flow after verifying both identities. Never assume the UUID values match. Existing FastAPI middleware must not reinterpret a Supabase `sub` as `users.id`.

For asymmetric Supabase signing keys, FastAPI should use a maintained JWT library and the project's JWKS at `https://<project-ref>.supabase.co/auth/v1/.well-known/jwks.json`, select a key by `kid`, allow only the configured expected algorithm(s), and refresh/cache keys safely for rotation. Validate the signature plus exact issuer `https://<project-ref>.supabase.co/auth/v1`, expected audience (confirm the deployed `aud`, commonly `authenticated`), expiration, applicable `nbf`/`iat`, and a valid UUID `sub`. Then map `sub` to `users.auth_user_id`, require the application profile to be active, and load club/role from the database. Do not authorize from `user_metadata` or an unreviewed `app_metadata` convention. Supabase's SSR guidance distinguishes verified `getClaims()`/`getUser()` from merely reading session state; use the server-side verified identity in Next.js too.

If the Supabase project still uses an HS256 signing secret, do not copy that shared secret to multiple services as a shortcut. Agree on a supported verification strategy with the Auth/backend owners (for example, moving to asymmetric signing keys or a server-mediated verified identity assertion). Do not weaken signature/issuer/audience/expiry validation, mint a second uncoordinated JWT, or change Member 3's existing JWT middleware without approval. JWKS/asymmetric key guidance: [Supabase JWTs](https://supabase.com/docs/guides/auth/jwts) and [Supabase signing keys](https://supabase.com/docs/guides/auth/signing-keys). Next.js server verification guidance: [Supabase SSR server client](https://supabase.com/docs/guides/auth/server-side/creating-a-client). Algorithm and issuer validation principles: [RFC 8725](https://www.rfc-editor.org/rfc/rfc8725.html).

## Migrations and approvals

Use the team's plain SQL migration runner and `schema_migrations` only after the backend owner confirms the combined branch's current migration head and ordering. Existing event migration `010_events.sql` is the known baseline; proposed `020+` MemberCore/FairSeat/GateSync numbers are not approval. The combined backend is reported to use local PostgreSQL, and compatibility with Supabase PostgreSQL has not been verified. Before any migration, inspect both branches' migration history, test forward application and rollback/repair strategy on disposable PostgreSQL, verify extensions/types/indexes/FKs and Supabase Auth schema permissions, then have owners review it. Never edit or renumber another member's migration.

Required decisions: (1) Member 2's category/capacity/sale-window contract and published-event transition rules; (2) Member 3's ticket payment, callback signature, idempotency, ledger, late-success and refund contract; (3) Supabase signing algorithm/JWKS and exact issuer/audience plus legacy JWT coexistence; (4) identity-linking ownership and `auth_user_id` uniqueness/provisioning; (5) club role matrix; (6) capacity scope, hold length, quantities, waitlist policy; (7) free-booking QR eligibility, cancellation/revocation and reporting quantity source; (8) migration owner, ordering, RLS and combined PostgreSQL/Supabase compatibility.

## Validation boundary

Local tests cover TypeScript request/response validation and safe transport behavior with an injected test transport. They do not contact FastAPI, Supabase, a payment provider, or PostgreSQL and cannot validate locks, idempotency durability, RLS, webhook signatures, or actual ticket purchases. Those require approved contracts and PostgreSQL/provider integration tests.
