# FairSeat ticket frontend

This package changes only `/bookings` and adds FairSeat-local UI, view models, tests and a server action. No global design, shared database, auth settings or teammate backend is changed.

## What works now

`/bookings` verifies Supabase identity with the existing `getUser` guard. Signed-out users return to `/login?next=%2Fbookings`; missing auth configuration fails closed. Public event access has not been approved. Authenticated users see an explicit disconnected event and reservation state, not fixtures.

Reusable UI accepts validated server presentation data for discovery/search/status/date filters, details, category/quantity, price review and reservation notices. Dates render in IST; the date filter explicitly uses UTC event date. React escapes text. Availability is informational, never a seat counter. Base-price estimates use integer-safe multiplication; actual member prices reuse MemberCore results in the existing quote contract. Expired or mismatched quotes cannot enable checkout.

## Connect only after approval

Replace the disconnected adapters in `server-integration.ts` with request-scoped approved adapters. Agree event read authorization and map actual event/category responses to `EventView` (a presentation model, not table columns). Use `cache: 'no-store'`; sanitize all responses before sending them to the browser. Connect the quote-read port to approved authenticated quote retrieval keyed by event/category/quantity using the existing quote type. The UI requests a fresh quote through a verified server action when selection changes, discards stale responses, and disables checkout while loading. It does not invent a FastAPI quote-generation endpoint.

The Next server action verifies identity on every submission then invokes `createBookingApi`. Approve the FastAPI endpoint, Supabase-to-application-user mapping and JWT audience/issuer verification before supplying server-only authorization. Do not simply forward an arbitrary session token to a guessed origin. The adapter must derive authorization from the current verified request, never a browser ID/role. Never put bearer tokens in props, URLs or logs. Keep approved endpoints in server configuration only. Keep Next Server Action origin/CSRF defaults intact; FastAPI must reauthorize, reprice and reserve atomically.

Only event/category selectors, quantity, retry key and accepted quote reference cross the browser command boundary. The backend must bind the quote and idempotency operation to its mapped purchaser. The UI keeps the same key for the same selection+quote while this page is mounted, blocks simultaneous submissions and caches completed responses. This is not durable idempotency across reloads. Add backend operation lookup before enabling cross-refresh recovery. On changed price, reload to obtain and accept a fresh quote; never silently retry with a new price.

Reservation notices use exact supported response states. `pending_payment` is displayed as held/awaiting payment; `waiting` is waitlisted. Existing lifecycle labels cover cancelled/expired/failed/refunded, but no booking-history endpoint is connected. There are no payment or QR actions: the current response contract provides neither a payable operation nor an issued ticket. A confirmed booking alone does not imply QR issuance.

## Validation limits

Tests render the actual React component on the server and exercise production view-model/controller/server-boundary code with injected test ports. Fixtures exist only in tests. Live event, quote, reservation, payment and ticket APIs remain unavailable. Test the connected adapter and real backend with a disposable authenticated account before claiming end-to-end booking works. Check narrow/wide screens and keyboard focus in the integrated frontend; CSS breakpoints and reduced-motion rules alone are not browser QA.
