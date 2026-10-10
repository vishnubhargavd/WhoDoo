# Memberships, Tickets, Check-in, Announcements, Certificates, Handover

Six tools that used to be listed as "coming soon" are built into the FastAPI app.

| Tool | Page | What it does |
|---|---|---|
| MemberCore | `/memberships` | Plans, joining and renewing, dues (online, in person, or waived), the member discount on tickets |
| FairSeat | `/tickets` | Book seats at the server's price; pay; held seats waiting for payment |
| GateSync | `/tickets/mine`, `/check-in` | A QR code per ticket; scanning and a guest list at the door; one admission per ticket |
| CrewDoo Connect | `/announcements` | Announcements by audience (everyone, members, volunteers, officers, one event), read tracking |
| CertiDoo | `/certificates`, `/verify` | Certificates from attendance or finished tasks, a public check by code, revocation |
| LegacyVault | `/handover` | Handover notes per role, with every saved version kept |

## Switching them on (local database only)

Their tables are in `migrations/pending/` and `make migrate` does not apply them. A tool whose
tables are missing answers 404 and stays listed as not here yet, so nothing changes for a
database that has not been migrated.

```
make seed-all        # wipe the local database, load demo data, add the modules and their demo data
# or, keeping your current local data:
make modules         # add the tables and demo plans, tickets, announcements and notes
```

Then in `.env`: `FAIRSEAT_ENABLED=1` and `FAIRSEAT_PAYMENTS_ENABLED=1` (Tickets and Check-in
need both the tables and the switch). Restart `make run`.

`scripts/migrate_pending.py` refuses any database that is not on this machine. Applying these
migrations to the shared Supabase database is a team decision and has not been made.

## How the parts connect

- **Membership to ticket price.** `membership_service.MemberCoreMemberships` is FairSeat's
  membership source. An active, unexpired membership of the event's club gives the discount
  its term was bought with. A pending membership gives nothing. `users.is_active_member` is
  never the pricing authority; MemberCore keeps that flag in step for merchandise pricing.
- **Dues to the ledger.** Dues paid online or recorded in person post one income entry
  (`source_module = membership`). A waiver posts nothing.
- **Booking to QR code.** A confirmed booking's seats get a credential at once. The code is
  `whodoo.ticket.v1.` plus 32 bytes derived from the ticket id under `TICKET_CREDENTIAL_SECRET`;
  only its SHA-256 digest is stored. The format and digest match `web/src/modules/gatesync`.
- **Check-in to certificates.** Participation certificates go to people GateSync checked in;
  volunteering certificates to people who finished a CrewPulse task on the event.

## Who can do what

| Action | Roles |
|---|---|
| Create or edit plans, record a cash payment, waive dues, cancel a membership | Treasurer, president |
| Scan and check in | Volunteer, event lead, treasurer, president |
| Post an announcement | Event lead, treasurer, president (remove: the author or the president) |
| Issue or revoke a certificate | Treasurer, president; an event lead for an event they lead |
| Write a handover note | President (any role); treasurer and event leads (their own role, volunteers, everyone) |

## Not built

- Refunding membership dues on request, and partial refunds. A payment that arrives for dues
  already settled another way is refunded automatically.
- Camera scanning needs a browser with `BarcodeDetector` (Chrome, Edge, Android). Elsewhere the
  door page takes a typed or pasted code, a handheld scanner, or a tap on the guest list.
- Announcements are read in the app; nothing is emailed or pushed.
- The Next.js app in `web/` still has its own scanner and booking pages and is not yet pointed
  at these endpoints.
- Stripe has only been exercised through the mock provider for dues, as for tickets.

## Tests

`make test-modules` (21 tests, on `whodoo_test`), or `make test` for everything.
