# WhoDoo integration recovery

This worktree is `work/whodoo-integration` in the Codex workspace, branch
`integration/whodoo-20261010`. Original `E:\WhoDoo` sources are untouched.
Only authorized push: `git push origin HEAD:refs/heads/Ameer`, non-force, after release checks.

## Completed Git operations

- Started from Ameer `cef9a82`.
- `20117ef`: merged Abhinav `98193f2`, which includes unified events/FairSeat `60ed7dc`.
- `0e7ca5f`: completed Joshith `69dc2ec` Supabase auth bridge merge; no conflicts.
- Vishnubhargav `1ac3186` equals main; there was no unique source to merge.
- General fetch hits a Windows case collision: `Joshith` and `joshith/fairseat-ticket-config`.
  Fetch the five explicit required branch refspecs; never prune or delete teammate branches.

## Initial audit and priorities

| Workstream | Initial verified source status | Priority |
|---|---|---|
| MemberCore | Auth UI/domain tested; persistence SQL proposed; frontend disconnected | P0 identity/schema compatibility, read adapter |
| FairSeat | Backend quotes, transactional capacity, holds and payments; frontend disconnected | P0 typed server adapters |
| GateSync | Domain/QR/scanner UI exists; backend issuance/redemption missing | P0 online secure redemption |
| TrustLedger | Club/event scoped immutable ledger backend/Jinja UI | Preserve; test payment idempotency |
| Snap-to-Reimburse | Event-linked claims, OCR and two-person approvals | P0 protect receipt access |
| MerchFlow | Conditional stock changes, images URLs, orders and payments | P0 provider refund finality; P1 manager workflow |
| Events | CRUD, lifecycle, ticket configuration, free-text venues | P1 controlled venues and calendar |
| CrewPulse | Tasks, assignments, blockers, audit history | Preserve and regression test |
| Autopilot/SyncSphere | Deterministic readiness and same-club schedule checking | Preserve; no venue reservation claim |
| Communication/CertiDoo/LegacyVault | No implemented module found in integrated sources | Missing; do not claim completed |
| PDF/email tickets | Missing | P2 after stable credentials/redemption |

Known bugs under repair: partial identity index rejected by auth catalog check;
refunded membership reactivation; merch refund success recorded before provider success;
public receipt URLs; silent Stripe-to-mock fallback; disconnected Next API ports.

## Runtime and validation state

- Dependencies installed into `.venv` and `web/node_modules`, never into original checkout.
- Previous frontend baseline: 158 tests passed. This is not integrated E2E evidence.
- Portable PostgreSQL 17.11 binaries under workspace `work/postgres/pgsql`, never a system install.
- Disposable data under workspace `work/whodoo-pgdata`; bind loopback port 55432 only.
- Keep PostgreSQL in a foreground exec session; ending the launcher kills child servers in this sandbox.
- Explicit test DSN: `postgresql+asyncpg://whodoo_test@127.0.0.1:55432/whodoo_disposable_test`.
- Auth/refund agents use separate `whodoo_auth_test`/`whodoo_finance_test` databases.
- Pytest rebuilds the dedicated test schema; NEVER point TEST_DATABASE_URL at Supabase/shared data.
- Normal migration runner only reads `migrations/*.sql`; all new migrations remain pending.
- Shared Supabase migrations, grants, RLS and Auth configuration were NOT applied or changed.

## Release gate

No push until integrated frontend typecheck/tests/build, backend regression and real
PostgreSQL booking/refund/QR race tests pass; final remote Ameer must remain an ancestor.
Live Supabase account linkage, real camera/browser E2E, transactional mail and Stripe
provider configuration must be reported separately from local fixtures and mocked providers.

## Checkpoint: 2026-10-11 01:20 IST
- GateSync backend and tests passed (8/8).
- MemberCore backend and tests passed (16/16).
- Auth Supabase integration tests passed (71/71).
- Frontend tests passed (158/158) and Next.js production build succeeded.
- Development-first mode activated for remaining features: calendar, venues, event expenses, inventory, images, PDF/email.

