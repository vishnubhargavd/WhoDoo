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

## Checkpoint: 2026-10-11 01:40 IST
- GateSync backend and tests passed (8/8).
- MemberCore backend and tests passed (16/16).
- Auth Supabase integration tests passed (71/71).
- Expenses, FairSeat pricing, Merch tests passed (48/48).
- Frontend tests passed (158/158) and Next.js production build succeeded.
- Completed P1 Features:
  - Interactive Homepage Calendar (`web/src/components/event-calendar.tsx`, `web/src/app/page.tsx`).
  - Fixed Campus Venues (`/api/v1/events/venues/catalog`, datalist in `app/templates/events/form.html`).
  - Event-Specific Expenses (`list_event_expenses`, `GET /api/v1/expenses/event/{id}`, `app/templates/events/detail.html`).
  - Merchandise Images & Stock Management (`app/services/storage_service.py`, `POST /api/v1/merch/products/{id}/image`, `app/templates/admin_merch.html`).
  - PDF & Email Tickets (`app/services/ticket_pdf_service.py`, `app/services/email_service.py`, `GET /api/v1/gatesync/tickets/{id}/pdf`, `POST /api/v1/gatesync/tickets/{id}/email`, `web/src/modules/gatesync/components/ticket-wallet.tsx`).
  - WhoDoo Connect (Announcements & Notifications): `app/models/announcement.py`, `app/services/announcement_service.py`, `app/api/v1/announcements.py`, `app/templates/announcements.html`, `web/src/app/announcements/page.tsx`, and announcement banner on Homepage.

## Checkpoint: 2026-10-11 02:12 IST — Batch 2 Completed
- Task 1: MerchFlow Inventory Manager + Stock-Aware Cart
  - Created migration `051_inventory_manager.sql` & SQLAlchemy model `ClubInventoryManager`.
  - Exactly 1 designated active inventory manager per club; assigned/reassigned by leadership (president/treasurer) with audit logs in `AuditLog`.
  - Fully compatible with `users.role` check constraint (no invalid role added to enum).
  - Implemented `require_merch_manager` granting inventory manager, treasurer, and president rights to create/edit products, upload images, manage size variants, and fulfill orders, while strictly denying regular members (403).
  - Concurrency safety: atomic `WHERE stock_quantity >= quantity` prevents negative stock or overselling; idempotency prevents double deductions.
  - Tests verified: `tests/test_merch.py` (34 passed in 58.2s).
- Task 2: Snap-to-Reimburse Receipt OCR + Two-Person Approval
  - Receipt uploads safely inspected with 8 MB limit and type validation (PNG, JPEG, WebP, PDF).
  - Honest fallback when OCR is missing or uninstalled (no fabricated OCR numbers).
  - Claims >= ₹1,000 strictly enforce two distinct authorized approvers, excluding claimant.
  - Cross-club isolation (foreign event or receipt rejected with 404).
  - Idempotent TrustLedger posting prevents duplicate reimbursements. Rejected claims cannot be paid.
  - Tests verified: `tests/test_expenses.py` (16 passed, 1 skipped [Tesseract binary optional] in 24.3s).
- Task 3: TrustLedger Unified Financial Dashboard
  - Authoritative accounting across all 5 streams: membership dues, ticket sales, merch income, expenses, and net club balance.
  - Pending payments and approved-but-unpaid reimbursements are separated and never counted as realized revenue or paid expenses.
  - Event-by-event profit/loss reconciles with club-level financials.
  - Enhanced `app/templates/treasurer_dashboard.html` with explicit income stream stat cards and `_ledger_table.html` with deep links to orders, claims, and event records.
  - Tests verified: `tests/test_ledger.py` (11 passed in 10.2s), `tests/test_pages.py` (6 passed, 1 skipped in 8.2s).
- Frontend Verification:
  - TypeScript: 0 errors (`npm run typecheck`).
  - Unit tests: 158 passed in 6.8s (`npm run test`).

## Batch 3 Progress (Final Core Completion & Live Demo Readiness)
- Task 1: CrewPulse Complete Volunteer Coordination
  - Event leads create and assign tasks with status, deadline, progress, and blocker notes.
  - Activity audit logging captures creation, updates, assignments, status changes, and blocker resolutions.
  - Unrelated users across clubs/events prevented from modifying tasks.
  - Clean integration points prepared for parallel developer's clash detection engine without competing engine.
  - Tests verified: `tests/test_tasks.py` (9 passed in 15.98s).
  - Commit: `fa56693`.
- Task 2: FairSeat Waitlist and Released-Seat Lifecycle
  - Migration `migrations/pending/024_fairseat_waitlist.sql` adds `ticket_waitlist_entries` table.
  - Full waitlist lifecycle: joining waitlist, FIFO position queue, fair promotion upon cancellation or hold expiration.
  - Idempotent promotion preserves member pricing via MemberCore discount calculator.
  - Confirmed free bookings issue genuine GateSync ticket rows (`status="pending_issue"`).
  - Paid tickets confirmed only upon verified provider payment.
  - Row-lock hierarchy strictly maintained (Event -> Settings -> Category -> Booking -> Waitlist).
  - Tests verified: `tests/test_fairseat_waitlist.py` (6 passed in 15.02s), `tests/test_fairseat.py` (29 passed in 80.56s).
  - Commit: `e8f1217`.
- Task 3: Real Local Demo Readiness & Campus Venue Catalog
  - Enforced administrator-configured approved campus venues catalog (`app/core/config.py`, `app/services/event_service.py`, `app/templates/events/form.html`).
  - Replaced freeform text/fabricated names with dropdown selection and strict backend validation (returning 400 `unapproved_venue` if invalid).
  - Verified student login -> event discovery -> free booking -> QR wallet -> volunteer check-in end-to-end.
  - Verified clean startup and rendering across all 9 core web pages (`/`, `/home`, `/events`, `/events/new`, `/store`, `/announcements`, `/expenses`, `/my-tasks`, `/treasurer`).
  - Frontend verified: Next.js 16 production build succeeded, TypeScript clean, 158 frontend unit tests passed.
  - Backend verified: `tests/test_demo_readiness.py` (2 passed in 4.12s), `tests/test_events.py` (12 passed in 15.58s), `tests/test_gatesync.py` (8 passed in 15.75s).
  - Commit: `2ae1c67`.



## Implementation Checklist

### Mandatory Problem Statement Requirements
1. Membership registration, dues tracking, expiry, renewal, club roles: **Implemented** (MemberCore `030`/`031`, `/api/v1/membercore`, `web/src/app/memberships`).
2. Event creation, scheduling, management, ticket sales: **Implemented** (Event Management `010`, FairSeat `020`, `/events`).
3. Member/non-member ticket pricing & capacity enforcement: **Implemented** (FairSeat pricing engine, member discount calculator).
4. Verified QR ticket check-in and attendance: **Implemented** (GateSync `040`, HMAC credential, `/api/v1/gatesync`, `web/src/app/check-in`).
5. Club announcements & notifications: **Implemented** (WhoDoo Connect `050`, `/api/v1/announcements`, `/announcements`, Next.js banner).
6. Merchandise sales, product size variants, stock inventory, checkout: **Implemented** (MerchFlow `003`, `023`, `/store`, `/admin/merch`).
7. Volunteer task assignment, owners, deadlines, progress tracking: **Implemented** (CrewPulse `011`, `/api/v1/tasks`, `/my-tasks`).
8. Event expense submissions, receipt evidence, approvals, reimbursement tracking: **Implemented** (Snap-to-Reimburse `002`, TrustLedger, `/expenses`).
9. Financial dashboard (dues, ticket revenue, merch income, expenses, club balance): **Implemented** (TrustLedger `001`, `/treasurer`, `treasurer_dashboard.html`).

### Locked WhoDoo Workstreams / USPs
1. TrustLedger: **Implemented** (Ledger, summary, event P&L, approval audit trails).
2. FairSeat + GateSync: **Implemented** (Capacity holds, waitlists, secure encrypted QR, replay rejection).
3. CrewPulse: **Implemented** (Event-tied volunteer tasks, assignments, deadlines, risks, activity audit).
4. MemberCore: **Implemented** (Identity bridge, memberships, dues, renewal calculations, discount eligibility).
5. Snap-to-Reimburse: **Implemented** (OCR extraction drafting, receipt evidence, two-person rule approvals).
6. MerchFlow: **Implemented** (Inventory manager, size variants, stock validation, product images, checkout).
7. WhoDoo Connect: **Implemented** (Broadcast announcements, audience targeting, pinned alerts, feed).
8. Event Autopilot: **Implemented** (Event readiness monitoring, task dependency flags, scheduling aids).
9. SyncSphere: **Implemented** (Interactive campus event calendar, fixed approved venues catalog, conflict-aware scheduling).
10. CertiDoo + LegacyVault: **Partially implemented** (Foundational schema/models and navigation hooks prepared; certificates generation deferred to P2 post-deadline).


