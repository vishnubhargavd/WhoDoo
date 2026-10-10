# FairSeat Phase 1

`decideBooking` evaluates one ticket request using a trusted server event snapshot,
server timestamp and existing MemberCore `DiscountResult`. These local inputs do
not define shared database columns. Timestamps are Unix milliseconds; prices are
integer paise. No dependencies, framework, database, payments or authentication
are added.

Opening is inclusive. Closing and event start are exclusive; closing cannot be
after event start. Invalid snapshots are rejected before cancellation/window
checks. Cancelled, started or closed events cannot accept waitlist requests.
Exactly full capacity is sold out or waitlist-eligible according to configuration.
Zero capacity follows the same rule. Overcapacity is an invalid snapshot, not a
new booking or waitlist opportunity.

Pricing must come from MemberCore for the same trusted event, user and timestamp.
The engine validates numeric shape and result consistency; it does not recompute
discounts or prove quote provenance. Never deserialize client availability or
membership flags into trusted inputs. Waitlist prices are display quotes and are
not locked. Later promotion must recalculate eligibility and price.

The engine does not allocate seats. Multiple calls can all report availability
for the same snapshot. Real enforcement requires an atomic PostgreSQL transaction
that re-reads trusted data, serializes event allocation and writes the booking.

Run `npm run typecheck` and `npm test` from `web/`.
