# WhoDoo web foundation

## MemberCore Phase 3A: profile and membership dashboard

`/account` presents the authenticated email, Supabase identity status, email
verification (when returned by getUser), and the existing logout action. Both
`/account` and `/memberships` verify identity on the server before rendering
protected content. Missing configuration or failed verification never renders
private profile data. The original verifyAccount return contract remains intact.

`MembershipDashboard` is a reusable presentation component. Current pages always
pass a disconnected view: no memberships, roles, paid dues or discounts are
fabricated. It displays fields for plan, club, start/expiry dates, status, member
discount and renewal reminders, each unavailable until connected.

`MembershipViewRecord` is a local UI contract, not a SQL or shared entity contract.
It reuses the existing Membership type. A future trusted server adapter can pass
a validated record to buildMembershipView with an explicit server timestamp.
The existing calculator validates dates, status and basis points and determines
eligibility; this view adds only presentation and a 30-day reminder. Validation
does not prove data provenance or authorize access. Never supply Auth metadata,
form values or browser membership flags as records. Additional properties such
as roles or paid-dues claims are not selected or displayed. Invalid records fail
closed to an unavailable view, never to an active membership.

No database adapter, users mapping, SQL, RLS or Supabase table requests are added.
Shared identity mapping, membership schema/history selection and club access
controls still require team approval. Before that, locally apply this patch and
check responsive profile/membership pages with your working authenticated session.
After approval, the smallest next task is a read-only membership adapter scoped
to the approved application-user/club mapping and covered by repository tests.

Actual Phase 3A checks on the isolated patch: npm run typecheck passed; npm test
passed 75/75 (63 original + 12 profile/dashboard tests); webpack production build
results are recorded in the patch's APPLY.md. Existing tests remain unchanged.
Real browser layout/session behavior was not verified in Codex. The reported
working Supabase login was user-provided context, not an auth test by Codex.

Earlier phase notes below document those phases' scope and limitations.

## MemberCore Phase 2C: email authentication

`/signup` and `/login` use the existing Supabase browser client. Signup sends
an email confirmation redirect to `/auth/callback?next=...`. The callback exchanges
the PKCE code, verifies identity with `getUser`, and sets server cookies. The
`/account` server page verifies with `getUser` before showing any identity data.
Logout is a POST Server Action and signs out the current browser session.
No application-user profile, club membership or role is created by signup.
Supabase Auth IDs are not existing application user IDs.

For local development, the team must allow the exact redirect
`http://localhost:3000/auth/callback?next=%2Faccount` in Supabase Auth URL
Configuration, and any other explicitly supported destination used in testing.
For deployment, allow only the deployed HTTPS callback URL. Keep Confirm email
enabled. Use the standard confirmation email template containing ConfirmationURL;
no custom token-hash template is required for this PKCE flow. Do not change a
shared custom template without coordinating with the team. A template that sends
users elsewhere must be reconciled before testing.

Open confirmation links in the same browser used for signup so its PKCE verifier
is available. If the link is expired, reused or opened in another browser, the
login page shows a recovery message. If confirmation has already completed at
Supabase, sign in with the password; otherwise retry signup after the email rate
limit allows it. Duplicate signup attempts use neutral wording to avoid claiming
that a new account was created. Email delivery/SMTP restrictions and rate limits
are controlled by the shared Supabase project, not this UI.

Redirects allow only `/`, `/account`, `/memberships`, `/bookings`, `/check-in`.
Unknown paths, query strings, fragments, URL encodings and external URLs fall
back to `/account`. Callback responses use a relative Location and no-store/no-
referrer headers. Auth errors are mapped to fixed messages; raw errors and tokens
are never displayed or logged by application code. Next dev request logging is
disabled to avoid recording callback codes; deployment access logs must likewise
redact auth callback queries. No privileged clients or custom JWT verification
are introduced. Existing calculator, repository, pricing service and FairSeat
business rules remain unchanged.

Validation for the isolated Phase 2C patch: 63/63 tests and TypeScript checking
passed. Default Turbopack build failed due to sandbox filesystem permissions;
`npm run build -- --webpack` passed. Production server startup passed. HTTP page
requests and the configured project's read-only Auth settings request were
blocked by sandbox network permissions. Live email/authentication, browser
rendering and cookie persistence have not been verified. No disposable user was
created. No dependencies were added or changed.

Local acceptance: run typecheck/test/build, start the frontend, use a disposable
email address you control, confirm the email in the same browser, inspect the
account page, sign out, then confirm `/account` redirects to login. Check invalid
passwords, duplicate signup, an expired confirmation link, and missing config.
Never paste passwords, keys, callback codes or tokens into test reports.

The earlier foundation notes below describe that phase's scope and verification.

Requires Node.js 24+. Next.js App Router, React and Tailwind provide a minimal
public shell. MemberCore and FairSeat business rules remain independent of the
framework. Authentication flows, application tables and database access are
not implemented.

```powershell
npm install
npm run typecheck
npm test
npm run build
npm run dev
```

The calculator lives in `src/modules/membercore/ticket-discount.ts`. The built-in
Node test runner executes TypeScript directly without another test dependency.

Inputs use integer paise, basis points, nonempty exact club IDs, strict UTC ISO
date strings (`YYYY-MM-DDTHH:mm:ssZ` or milliseconds `.sssZ`), and an explicit Unix
timestamp in milliseconds. Invalid supplied data throws, even if the membership
is inactive. Start is inclusive, expiry exclusive. Final price is rounded to the
nearest paise, with half-paise ties upward, using BigInt intermediate arithmetic.

Eligibility and discount application are separate: an eligible zero-discount
membership receives the full price. A tiny discount can also round to no savings.
If multiple ineligibility reasons apply, precedence is inactive status, wrong
club, future validity, then expired validity.

This function is a quote calculator, not authorization. Future callers must
load membership from trusted server data and use a server clock. Never accept
client membership flags as authoritative. Booking transactions must revalidate.

Phase 2A adds a read-only repository interface and `quoteMemberTicket` service.
Application user IDs and Supabase Auth user IDs have distinct local TypeScript
types. Labelling an application ID does not authenticate it; the caller must
supply server-verified context after the approved identity mapping. No shared
identity contract, authentication flow, database or membership adapter is implemented.

The service checks trusted club scope, retrieves membership by application user
and club, and delegates all pricing and eligibility to the unchanged calculator.
A missing record returns the calculator's full-price quote. Wrong-user or
wrong-club repository records and storage failures throw stable errors; storage
errors never fall back to a guest quote. The future adapter must define how
renewal/history rows produce one deterministic record under the approved schema.

Tests use an in-memory repository. `npm run typecheck` also verifies that an Auth
user ID cannot be passed to the repository in place of an application user ID.
Business tests require no additional runner. Importing `.ts` paths and erasable TypeScript
syntax support the existing Node 24 test runner without a compilation step.

## Frontend and Supabase setup

Routes are `/`, `/memberships`, `/bookings`, and `/check-in`. They show explicit
unfinished placeholders and no fabricated organization data. Minimal styling
is temporary pending the shared design system.

Copy `.env.example` to `.env.local` only if `.env.local` does not already exist.
Fill `NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` privately
from the shared project. Never put a secret or service-role key in these fields.
Local env files are ignored by Git. Restart development after changing them;
rebuild deployment artifacts because public variables are inlined at build time.

Without these variables the pages display a setup message. With valid-looking
configuration they say configuration is present, not that connectivity or
authentication has been verified. This foundation makes no application queries.

`src/lib/supabase/client.ts` creates the browser client; `server.ts` is server-only
and creates one cookie-aware client per request. `src/proxy.ts` uses the Next.js
16 proxy convention. It verifies/refreshes existing tokens with `getClaims`,
propagates refreshed request/response cookies and cache headers, and prevents
session response caching. All current pages are public. This is not authorization:
future actions must verify identity and load club roles from approved trusted data.
Unverified `getSession` data and editable user metadata cannot grant access.

Application user IDs remain distinct from Supabase Auth IDs. Mapping, shared
schema, RLS, migrations and finance JWT integration require team approval.
The next authentication task should implement email sign-in, sign-out and safe
confirmation redirects with mocked tests and staging verification. Connecting
users or memberships must wait for the approved mapping and schema.

## Validation of this isolated patch

Original checkout: typecheck passed and all 39 business tests passed.
Prepared source: all 45 tests passed, including six configuration-validation tests.
Dependency installation failed with ENOTFOUND for registry.npmjs.org in Codex.
Full frontend typecheck could not complete (missing Node type definitions);
build and development start could not run (Next executable missing).
Browser rendering and a live Supabase connection have not been verified.
Run the commands above after dependency installation before treating the shell
as verified. The existing lockfile must be updated by successful `npm install`.
