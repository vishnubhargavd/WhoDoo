# WhoDoo MemberCore Phase 3C — compatibility review package

**Status: proposed, unapplied, and not database-tested.** The package is isolated under the Codex workspace. `E:\WhoDoo`, Member 2/3 code, Git branches, and Supabase were not changed.

## Confirmed inputs and limits

This correction uses the compatibility facts supplied for this task: the backend runner is `app/db/migrate.py`, a plain SQL runner using asyncpg; FairSeat exposes an asynchronous `MembershipSource`; the existing TrustLedger stores rupees as `numeric(12,2)`; 030 and 031 are proposed ordering numbers; and the target branch checkpoint is `f3b308c`. I did not inspect the backend checkout, runner implementation, migration discovery path, or database. Therefore the active migration directory behavior, runner SQL execution details, backend models, and resolved schema are still to be confirmed by Member 3.

The previous Phase 3B proposal and the existing TypeScript MemberCore calculator were available in the Codex workspace and were reviewed. The calculator uses start-inclusive/expiry-exclusive eligibility and nearest-paise rounding with ties upward. The supplied 379-vector Python/TypeScript parity result was not rerun.

## Package files

- [`migrations/pending/030_membercore_identity.sql`](migrations/pending/030_membercore_identity.sql) — portable SQL; adds nullable `users.auth_user_id` and uniqueness indexes without changing existing IDs or `is_active_member`.
- [`migrations/pending/031_membercore_memberships.sql`](migrations/pending/031_membercore_memberships.sql) — portable membership plan/current membership/renewal/dues/reconciliation/activation/audit schema and integrity triggers.
- [`supabase_membercore_security.sql`](supabase_membercore_security.sql) — separate, Supabase-only FK, grants, RLS, and private identity helper proposal.
- [`membercore_migration_assertions.test.sql`](membercore_migration_assertions.test.sql) — psql-compatible transaction/rollback assertions for disposable PostgreSQL.
- [`supabase_security_assertions.test.sql`](supabase_security_assertions.test.sql) — Supabase catalog-level security assertions.
- [`supabase_rls_isolation.test.sql`](supabase_rls_isolation.test.sql) — local-Supabase runtime checks for cross-user, cross-club, unmapped-user and unauthorized-write isolation.
- [`fastapi_membercore_contract.py`](fastapi_membercore_contract.py) and [`FASTAPI_HANDOFF.md`](FASTAPI_HANDOFF.md) — exact async FairSeat interface and service contract.

No installer is included; this package must remain under review and the migrations stay in `pending` until approval.

## Plain SQL runner integration

The portable files are named and ordered for `migrations/pending/` and contain **no `BEGIN` or `COMMIT**. The runner owns migration transactions. Do not place either file in the active migration directory or run them until approved.

Before integration, Member 3 should inspect `app/db/migrate.py` and confirm how it discovers active SQL files, whether it orders by filename, and how it stores applied migration IDs. On approval, move 030/031 from `migrations/pending/` into the exact active location discovered in that runner; run 030 before 031. Keep the migration file names unchanged and let the runner's transaction wrapper manage commit/rollback. Do not add a second migration ledger or touch `auth.schema_migrations`. Keep the Supabase security add-on separate from portable SQL and integrate only after approving its Supabase-specific prerequisites.

The test files are distinct from migrations and intentionally use `BEGIN`/`ROLLBACK`. Run them only against a disposable database after applying the complete approved shared SQL chain plus proposed MemberCore migrations. The Supabase security script is also a proposal; do not run it against the live project.

## Schema and compatibility decisions

030 preserves `public.users.id`, existing references, roles, and `is_active_member`. `auth_user_id` is nullable and unique only when populated; there is no email auto-linking and no `auth.users` reference in the portable migration. A second unique index on `(id, club_id)` supports club-consistent composite FKs.

031 binds plans, memberships, renewals, dues, reconciliations and activation grants to the existing users/clubs. `memberships` remains one current-state row per `(user_id, club_id)`; renewals are immutable history. The database requires active status to match the latest renewal's plan, validity window and purchased discount snapshot, with a valid paid-settlement or complimentary-waiver activation grant.

`discount_bps_snapshot` is stored on both current membership and each renewal. Editing a plan only affects future enrollments/renewals; it cannot retroactively change the purchased term. Renewal copies the then-current plan basis points into a new immutable history row and updates the current snapshot only after that renewal is activated.

Membership plan price and dues amount are integer paise capped at **999,999,999,999 paise**, equivalent to TrustLedger's maximum `numeric(12,2)` rupees value INR 9,999,999,999.99. Convert with Python `Decimal(paise) / Decimal(100)`, never float. The earlier JavaScript safe-integer cap was too large for TrustLedger and has been corrected. Membership dues and reconciliation remain entitlement/payment evidence, not a second TrustLedger accounting entry.

The proposal intentionally leaves `users.is_active_member` unchanged. The single approved backend writer must decide whether and when it mirrors a current active entitlement, after auditing its existing merchandise consumers. Membership status and date-bounded rows remain the discount source of truth.

## Activation policy

- Paid activation: the current membership begins `pending`. FastAPI verifies provider signature, provider account, amount, currency, due/renewal association and successful settlement. In one transaction it records one idempotent paid reconciliation, creates the linked activation grant (which the DB requires to reference that paid event), writes audit evidence, and changes the current membership to active. A failed result cannot activate and a failed result after a paid settlement is rejected.
- Complimentary activation: no fake charge or paid event. FastAPI verifies an authorized same-club waiver actor under the team-approved role policy, then writes a waiver activation grant with reason and idempotency key. The database enforces same-club actor linkage, requires the reason, and writes an audit event. The backend role check remains necessary; the existing role set does not settle who is allowed to waive dues.
- Refund: a refund is accepted only after a verified paid reconciliation. A unique full-refund event cancels the matching current membership term in the same transaction and records an audit event. Refunding an older term does not cancel a newer current term. Partial refunds need a separate approved contract.
- Retry behavior: unique enrollment, renewal, due, provider-event, activation-grant and request keys prevent duplicate effects. Lock current membership/due rows and recheck state in the transaction. A duplicate event returns its stored result. A failed initial payment stays pending. A failed future renewal does not extend the prior paid term; that prior term remains eligible only until its existing expiry. A refund of that current term cancels it.

Database protections include matching same-club FKs, valid date and discount/amount checks, uniqueness, immutable histories, settlement proof for paid activation, waiver record requirements, an activation proof before `active`, and refund-driven cancellation. Backend protections include verified JWT and account mapping, fresh role/club authorization, webhook signature and amount verification, waiver authority, row locking/idempotency orchestration, and never trusting browser membership flags. A SQL row cannot independently prove a remote provider signature is valid.

## Supabase-only security review

The separate SQL adds `users.auth_user_id REFERENCES auth.users(id) ON DELETE RESTRICT`; Auth deletion must be preceded by an explicit unlink and cannot cascade into application identity. The private helper returns only the caller's mapped app ID and club using `auth.uid()`, takes no caller-controlled identifiers, sets `search_path = ''`, and schema-qualifies relations. Execute is revoked from `PUBLIC` and `anon` and granted only to `authenticated`. It does not alter RLS or grants on existing `users`, `clubs`, events, TrustLedger, or volunteering tables.

The helper uses `SECURITY DEFINER` because authenticated clients cannot read the existing users mapping row. Its owner must be deliberately reviewed and have `BYPASSRLS` (normally a controlled migration owner); otherwise forced RLS on `public.users` could hide the mapping. This is a narrow identity lookup, not a general users-table bypass. Supabase recommends fixed search paths, schema-qualified relations, explicit execute grants, and careful review of definer privileges. [Function security](https://supabase.com/docs/guides/database/functions) · [RLS](https://supabase.com/docs/guides/database/postgres/row-level-security).

Authenticated users get read-only access to their own membership, same-club plan display and limited reconciliation fields. Unmapped users see no membership rows. Staff administrative operations remain FastAPI-only with fresh role/club checks. No client role or membership writes are granted. Catalog checks are not a substitute for the separate local-Supabase runtime isolation script.

## Tests and actual validation

`fastapi_membercore_contract.py` passed Python syntax compilation using the bundled Python runtime. No PostgreSQL server tooling was available: `psql`, Docker, and Supabase CLI are unavailable. Therefore neither migration nor either SQL assertion script was executed or server-parsed. The runtime Supabase test requires four disposable Auth fixtures and was not run. No frontend files changed; frontend checks were not rerun. No live database query or migration occurred.

Disposable portable test command after the approved chain and both portable migrations are installed in a throwaway database:

```powershell
$env:MEMBERCORE_TEST_DSN = 'postgresql://...disposable database only...'
psql -v ON_ERROR_STOP=1 -d $env:MEMBERCORE_TEST_DSN -f '.\membercore_migration_assertions.test.sql'
```

For `supabase_rls_isolation.test.sql`, create four Auth users in **local Supabase only**, then pass their UUIDs to psql as `auth_a`, `auth_b`, `auth_c`, and `auth_unmapped`. The script seeds three mapped app users (two in one club, one in another), checks own-row isolation and unmapped denial, attempts client INSERT/UPDATE/DELETE, then rolls back. It sets local JWT claim GUCs for RLS testing; it does not test JWT signature verification.

## Remaining approvals before activation

1. Member 3 confirms the actual runner discovery/order/transaction behavior and shared schema definitions; approve moving pending files to the active migration location only after disposable tests.
2. Confirm the FastAPI DSN is the intended shared Supabase PostgreSQL database, the `numeric(12,2)` rounding/casting convention, and the TrustLedger bridge owner.
3. Approve account linking and Auth deletion/unlink behavior; never email-match automatically.
4. Approve which same-club roles may grant complimentary waivers and perform payment reconciliation.
5. Approve full-refund semantics, partial-refund handling, status/`is_active_member` synchronization and plan-change treatment (snapshot policy proposed above).
6. Review Supabase helper owner, BYPASSRLS, grants, Data API exposure, and all RLS policies with local tests/advisors.
7. Run both rollback-only SQL assertions and local Supabase cross-user/cross-club/unmapped/unauthorized-write tests. These remain unexecuted.

Stop here: this package is for review. Do not install or deploy it until the approvals and disposable test results are complete.
