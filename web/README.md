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

Validation in the preparation environment: all 15 automated tests passed on
Node 24.21.0. Type checking could not execute because TypeScript was unavailable
and npm installation failed with ENOTFOUND for registry.npmjs.org. Run the three
commands above locally before considering this component fully verified.
