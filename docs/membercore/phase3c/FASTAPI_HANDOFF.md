# FastAPI handoff: MemberCore and FairSeat

This is an interface and behavior contract for Member 3 to implement in the existing backend. It does not add or replace the FairSeat pricing service, JWT verifier, SQLAlchemy models, or live payment endpoint.

## Exact FairSeat interface

The backend module is `app/services/fairseat_pricing.py`. Keep its existing asynchronous protocol and result type exactly:

```python
async def membership_for(
    self,
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    club_id: uuid.UUID,
    now: datetime,
) -> Membership | None:
    ...
```

`Membership` remains Member 3's existing result with `status: str`, `club_id: str`, `starts_at_ms: int`, `expires_at_ms: int`, and `discount_bps: int`. See `fastapi_membercore_contract.py`, which imports rather than duplicates `Membership`.

Resolve `user_id` from a verified Auth subject to `public.users.id`; never use `auth.users.id` as `user_id`. Query the membership/current plan data by both `user_id` and `club_id`. Return the membership's `discount_bps_snapshot`, not the plan's current discount. Emit `club_id` as its string form, timestamps as UTC epoch milliseconds, and `now` must be timezone-aware UTC. A deterministic integer millisecond conversion is `delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000` for `delta = utc_datetime - datetime(1970, 1, 1, tzinfo=UTC)`.

A `pending` membership must not grant a discount. Return its pending status only if the existing pricing calculator explicitly treats it as ineligible; otherwise return `None` until Member 3 adds a parity-tested pending case. Never map pending to active. Do not rewrite the existing price formula or rounding behavior. On lookup failure, fail closed according to the existing FairSeat policy; do not synthesize an active membership from `users.is_active_member` or browser input.

## Supabase identity and FastAPI authorization

1. Verify the Supabase user access token cryptographically against the configured project's JWKS. Select the signing key by `kid`, require an algorithm compatible with that key, and fail closed for unknown keys. The project is reported to use ES256; never fall back to HS256 or accept `alg=none`.
2. Validate exact issuer, audience `authenticated`, `exp`, `iat`, optional `nbf`, and UUID `sub`. Bound JWKS cache lifetime and support key rotation.
3. Treat verified `sub` only as `auth.users.id`; resolve `public.users.auth_user_id = sub`, then pass the row's existing `public.users.id` to MemberCore, FairSeat, events, and finance. Reload current club and role from PostgreSQL for each authorization decision.
4. An unmapped Auth user has no application profile, membership or club access. Never fall back to email, editable `user_metadata`, browser role, request `user_id`, or request club ID.
5. Keep a legacy finance-token verifier as a separate explicit path only during an approved migration window. Do not reinterpret one token family under the other's subject contract.

Member 3 owns verifier implementation and dependencies. Verify the issuer/JWKS configuration and current signing algorithm against the target project at implementation time; those facts were supplied by the team and were not queried for this package.

## Exact activation policy

### Paid membership

- Creating an enrollment creates/updates a `pending` current membership, appends its renewal snapshot, and creates a dues obligation. Pending never grants member discounts.
- A payment callback is not trusted based on payload fields alone. Verify provider signature, provider account, amount, currency, renewal/dues relationship, and successful settlement status server-side.
- In one database transaction, lock the dues and membership rows; insert one `paid` reconciliation with unique provider event ID and request/idempotency key; create the `paid_settlement` activation grant pointing to that exact reconciliation; append audit evidence; then update current membership dates, plan, discount snapshot and status to `active`.
- The database trigger rejects `active` unless the latest renewal matches the current membership window/plan/discount and has an activation grant; the grant trigger requires an existing `paid` reconciliation for the same renewal. Unique constraints serialize duplicate provider events and duplicate settlements.
- If the verified event is a failure, record a `failed` reconciliation and leave a new enrollment `pending`. A failed renewal attempt must not extend the prior paid period. An already active, previously paid period remains eligible only through its existing expiry. A failed event after a paid settlement is rejected.

### Complimentary membership

- Do not create a fake payment, zero-value `paid` event, or provider reference to simulate settlement.
- In one transaction, an authorized same-club actor records a `complimentary_waiver` activation grant with a required reason and idempotency key; the database checks the actor belongs to the same club and writes an audit event. The backend must freshly authorize the actor's role against the team-approved waiver policy; a same-club foreign key alone does not prove authorization.
- Update the current membership to active only after the waiver grant exists. The DB activation trigger permits the transition only with that grant.

### Refunds and corrections

- A refund reconciliation is accepted only after a recorded paid settlement. The DB permits one full refund for the dues row and automatically cancels the matching current entitlement in the same transaction. A refund for an older period does not cancel a newer, separately paid period.
- Partial refunds are not represented by this proposal and need a reviewed contract. Do not mark refund as an edit to the paid row; append the verified refund event.
- Renewal, dues, reconciliation, activation-grant and audit evidence is immutable. Correct it with a compensating event. Idempotent retries return the already recorded outcome and do not create a second renewal, grant, or settlement.

**Database-enforced:** same-club FKs, uniqueness/idempotency keys, valid amounts and dates, discount bounds/snapshots, immutable history, settlement proof before paid activation, a grant before active status, same-club waiver actor, refund-after-paid, full-refund uniqueness, and automatic cancellation of the refunded current term.

**Backend-enforced:** JWT signature/claims, app-user mapping, current role and club authorization, approved waiver roles, provider webhook signature and amount verification, transaction ordering/row locks, no trusting client flags, and correct updates to existing membership/current-state fields. The portable DB cannot independently know whether a remote provider signature is genuine.

The existing `users.is_active_member` flag remains unchanged by the SQL. The single approved backend writer must decide when it mirrors the current entitlement and update it in the same transaction as activation/cancellation/refund; it must never be used as the pricing authority. Audit its merchandise consumers before enabling this compatibility write.

## Purchased discount terms

This proposal adds `discount_bps_snapshot` to both `memberships` and `membership_renewals`. On enrollment or renewal, copy the selected plan's current basis points into the renewal and current membership row within one transaction. Editing a plan changes only future enrollments/renewals; it does not alter a purchased term. A renewal deliberately buys the new snapshot. The active-membership trigger requires the current row to match the latest renewal's snapshot. Tests must change the plan after enrollment and prove the current membership and its prior renewal remain unchanged, then verify a later renewal records the new rate.

## Money and TrustLedger

TrustLedger uses `numeric(12,2)` rupees. Its maximum representable amount is INR 9,999,999,999.99, or **999,999,999,999 paise**. Both membership plan price and dues amount use this cap (not JavaScript's much larger safe-integer maximum). Convert integer paise to exact decimal rupees using `Decimal(amount_paise) / Decimal(100)`; never use binary float. Membership dues/reconciliation are operational entitlement evidence and are not a second TrustLedger accounting entry. Any ledger integration is a separate, explicitly approved bridge owned with Finance.

## Booking integration

The booking request does not include trusted membership status, discount, final price, user ID or club ID. FastAPI verifies the caller, resolves the application user and event from database rows, calls the existing `MembershipSource` with the exact keyword-only arguments above, and passes that trusted result into Member 3's existing FairSeat pricing/atomic reservation flow. Keep his tested calculator and rounding logic unchanged. The request's 379-vector parity report was supplied by the team and was not rerun here.
