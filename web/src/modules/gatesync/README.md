# GateSync Phase 1 — credential contract and check-in rules

This is a deterministic domain component, not a scanner or live check-in system.
No route, camera access, database adapter, table, migration, authorization check
or ticket redemption has been implemented.

## QR credential

Use a versioned opaque bearer credential with **32 random bytes from a
cryptographically secure server RNG**, base64url encoded, in the form
`whodoo.ticket.v1.<43-character-token>`. The QR contains only this random token:
no student name, email, user ID, membership, price, event or club data. Do not
log, display in diagnostics or persist the raw token. Deliver it only to the
ticket holder over an authenticated issuance flow. This phase validates its
format and computes a real SHA-256 digest; it does not issue tokens or pretend
the digest authenticates a token. Unpredictability comes from the future secure
random generator, and authenticity comes from finding its digest in trusted
server storage. A changed token produces a different digest and no match.

The local `TicketCredentialRecord` lookup contract contains credential version,
ticket ID, event ID, club/organization ID, issue time, expiry time, credential ID
(the lowercase SHA-256 digest), state, and optional redeemed time. These are
integration fields, **not agreed shared database columns**. Existing modules do
not publish reusable Ticket ID or Event ID types; GateSync aliases remain local
until the team approves a shared contract. Membership and personal data are not
needed for check-in.

## Decision inputs and outputs

`resolveCredential` validates the opaque token, hashes it, and requests a
read-only digest lookup. Unknown/malformed tokens return `invalid`. Repository
errors return `unavailable`, a distinct operational outcome which the typed
decision API rejects. A future route should map it to a retryable server error
rather than a ticket denial. It does not allocate, consume or redeem anything.

`decideCheckIn` accepts that resolution and server-trusted ticket/event state,
including a deterministic server clock. It returns one of `ALLOW`,
`INVALID_CREDENTIAL`, `WRONG_EVENT`, `WRONG_CLUB`, `NOT_YET_VALID`, `EXPIRED`,
`REVOKED`, `CANCELLED`, `ALREADY_CHECKED_IN`, or `EVENT_NOT_OPEN`. Lookup
`unavailable` is an operational error, never an eligibility decision. Ticket
issue time and event opening are inclusive; ticket expiry and event closing are
exclusive. Invalid and missing dates or IDs fail closed. Repeated calls with
identical inputs have identical decisions.

## Security limits and future integration

Ticket eligibility is separate from staff authorization. A future server route
must authenticate the scanner with Supabase Auth `getUser`/verified claims and
enforce club-scoped check-in permission using the team's approved trusted role
source. Never accept a client role, `isValid`, membership flag, event state or
`alreadyCheckedIn` flag as evidence. This pure function can only evaluate the
server state it receives. Two simultaneous calls can both return `ALLOW`.

Real duplicate-entry prevention requires one authenticated, transaction-safe
database operation that verifies the current event/club/ticket state and
atomically changes an unused ticket to redeemed (or conditionally updates only
where `checked_in_at IS NULL`). The transaction must return success to exactly
one request. Store a unique digest, link it to the existing FairSeat booking and
ticket, record check-in time and authorized scanner ID, and handle revoked,
cancelled, expired and reissued credentials. Never do a separate read then write.
The schema, row-level security, constraints, function permissions and migration
must be approved before implementation; this module invents none.

FairSeat must issue a credential only after a paid/confirmed booking transition
and persist its ticket/event/club link atomically under the approved schema.
Cancellation/refund policy must coordinate ticket revocation with FairSeat and
TrustLedger without changing their code here. QR issuance, rotation, scanner UI,
attendance views, staff roles and any notion of seat capacity remain out of scope.

Run `npm run typecheck`, `npm test`, and `npm run build -- --webpack` from `web/`.
Unit tests use opaque non-production fixtures and a read-only in-memory digest
lookup. They test rules and tamper-to-lookup failure, not live scanning, cryptographic
random issuance, database races, authorization or actual duplicate prevention.
