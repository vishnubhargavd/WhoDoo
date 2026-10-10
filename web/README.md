# MemberCore first component

Requires Node.js 24+. This is a minimal TypeScript project with no Next.js,
authentication, database, Supabase, or UI implementation yet.

```powershell
npm install
npm run typecheck
npm test
```

The calculator lives in `src/modules/membercore/ticket-discount.ts`. The built-in
Node test runner executes TypeScript directly; TypeScript is the only dependency.

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
identity contract, authentication, database or Supabase adapter is implemented.

The service checks trusted club scope, retrieves membership by application user
and club, and delegates all pricing and eligibility to the unchanged calculator.
A missing record returns the calculator's full-price quote. Wrong-user or
wrong-club repository records and storage failures throw stable errors; storage
errors never fall back to a guest quote. The future adapter must define how
renewal/history rows produce one deterministic record under the approved schema.

Tests use an in-memory repository. `npm run typecheck` also verifies that an Auth
user ID cannot be passed to the repository in place of an application user ID.
No new dependencies are required. Importing `.ts` paths and erasable TypeScript
syntax support the existing Node 24 test runner without a compilation step.
