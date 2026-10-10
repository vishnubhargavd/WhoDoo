# GateSync Phase 2A frontend integration

This phase adds a student ticket display, an authenticated mobile camera scanner, and a disabled-by-default server redemption adapter. It does not issue tickets, read a live ticket source, authorize an event operator, redeem credentials, or mutate attendance.

## Ticket display contract

The ticket list receives `null` today and renders “Ticket service not connected.” A future server-side read adapter may supply `TicketDisplayRecord` only after verifying the signed-in identity and querying the approved booking/ticket contract. The display view reuses GateSync's existing `isOpaqueCredentialFormat` parser, which is also now used by `digestOpaqueCredential`; it never mints or substitutes a code. A confirmed booking without an issued credential is shown as confirmed with no QR. Pending, failed/refunded, cancelled, revoked, checked-in, malformed, and unknown records never render the bearer credential. When issued, the QR is SVG rendered server-side. The raw value must not enter logs, URLs, analytics, browser storage, or an unauthenticated route.

## Camera and local scan behavior

`/check-in` now checks identity with the existing server `verifyAccount`/`getUser` flow before rendering the scanner. This authenticates an account only; it does not grant volunteer/event permissions. The route says explicitly that operator authorization and redemption are disconnected. Camera permission is requested only after the Start button. ZXing's `BrowserQRCodeReader` is dynamically imported after that gesture. Camera enumeration and switching appear only when multiple devices are found. Stop, a captured value, and component unmount stop ZXing controls and media tracks. Errors are separated into permission denied, no camera, and initialization/secure-context states.

The first scan pauses the reader. The raw result exists only for an in-memory format check and is discarded immediately. `credential_captured` means only that the opaque token matches the local versioned format; it does not establish existence, validity, event membership, authorization, or redemption. The result says “Credential captured — server verification unavailable. Attendance was not changed.” Invalid tokens receive generic feedback without showing the input.

The browser uses a pure browser-safe parser extracted from GateSync's existing credential module. The existing server SHA-256/digest lookup remains unchanged in behavior and stays in `credential.ts`; the client does not import Node crypto.

## Proposed FastAPI handoff (not connected)

`redemption-api.ts` is server-only. It is disabled unless `GATESYNC_REDEMPTION_URL` is set to an approved HTTPS endpoint. No concrete path is assumed. Its typed request sends the opaque credential and expected event selector in a POST body, with the Supabase access token supplied by the verified server caller as Bearer authorization; it uses `cache: no-store`, rejects redirects, never logs the credential, and accepts only a structured response. Callers must first verify the operator with server-side Supabase `getUser`, then have FastAPI independently validate the token, check the operator's club/event permission, resolve the credential digest, re-read booking/payment/ticket/event state, and atomically redeem it once. Only a structured `redeemed` response carrying a redemption ID and canonical server timestamp may produce the success UI. No browser role, client-selected identity, local eligibility result, or `ALLOW` from `decideCheckIn` is sufficient.

The eventual database operation must be one transaction/conditional state transition that lets exactly one concurrent request win. It must reject replay, refunded/cancelled/revoked/expired tickets and wrong-event scans; store only a digest; record verified time and operator application-user ID; and coordinate issuance/revocation with FairSeat payment/refund state. A successful response includes the redeemed event ID, redemption ID, server check-in timestamp and safe event label; compare the event ID with the expected event before presenting success. Endpoint path, request fields, response schema, event selection, migration, role contract and transaction owner require Member 2/3 approval.

## Device and build validation

Automated tests exercise parser shape, no-QR states, scanner state mapping, camera failure mapping, duplicate suppression, cleanup, auth guard, adapter contract parsing, and rendered accessibility/status output. They do not prove a physical camera works. Before production, test HTTPS on an iOS Safari device and Android Chrome: grant/deny/revoke permission, no-camera hardware, front/back camera switching, low-light/focus/rotation, navigation away while active, repeated scans, and network/authorization failures against a disposable backend. Verify the OS/browser indicator turns off after stop/navigation.
