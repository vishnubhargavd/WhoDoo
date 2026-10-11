# WhoDoo — Event Operations & Volunteer Intelligence (Member 2)

Built inside the shared FastAPI app (Member 3's foundation), using its auth, error format,
migration runner, layout and styles.

| Module | What it does |
|---|---|
| **Event Management** | Create, edit, publish, cancel and complete events. Title, description, start/end, venue, lead. Event page shows details, schedule and organizers (lead, creator, and the crew with each person's tasks done / at risk). Club-scoped, role-checked, audit-logged. |
| **CrewPulse** | Tasks per event: owner, deadline, priority, critical flag, status (to do → in progress → blocked → done) with blocker notes, last-updated / completed times and an activity trail. Volunteer "My tasks" dashboard with pending / in progress / blocked / completed counts. |
| **Event Autopilot** | Readiness score (0–100) from deterministic rules over real data. Every deduction shows why, the evidence, and the next action. No AI, nothing automatic. |
| **SyncSphere** | Detects overlapping events, double-booked venues and double-booked leads within the club, and suggests clash-free alternative slots. |

## Run it (Windows, PowerShell)

Needs Python 3.13 and PostgreSQL 14+ (databases `whodoo` and `whodoo_test`).

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
$env:DEMO_AUTH="1"; $env:JWT_SECRET="any-long-local-secret"; $env:PAYMENT_PROVIDER="mock"; $env:PYTHONIOENCODING="utf-8"
$env:DATABASE_URL="postgresql+asyncpg://localhost/whodoo"
.venv\Scripts\python -m scripts.seed_finance_demo --reset   # Member 3's people, events, money (local DB only)
.venv\Scripts\python -m scripts.seed_events_demo            # Member 2's schedules, tasks, a venue clash
.venv\Scripts\python -m uvicorn app.main:app --port 8000
```

Open http://localhost:8000/dev/login, pick **Sneha Iyer** (event lead) and go to **Events**.

Tests (they reset the `whodoo_test` database only):

```powershell
.venv\Scripts\python -m pytest -q
```

## Demo script (≈2 minutes)

1. **Sneha Iyer** → Events → **Spring Gala**: readiness 60/100, with a venue clash, a blocked
   DJ booking and two unowned tasks, each explained with a next step.
2. SyncSphere card: the clash with a draft event at the same hall (the draft's name stays hidden
   from her), plus three alternative slots. **Move here** reschedules the event and the clash disappears.
3. **New event** → create, add a critical task for **Arjun Das**, publish.
4. Switch user → **Arjun Das** → My tasks → **Report blocker**.
5. Switch back to Sneha: the task is flagged on the event page and in the list ("1 at risk"), and
   Autopilot drops the event out of "On track", giving the blocker as the reason.

## API (all under `/api/v1`, signed-in users only)

| Method | Path | Who |
|---|---|---|
| GET | `/events?scope=upcoming\|past\|all` | Club members (drafts only for president / lead / creator) |
| POST | `/events` | President, event leads (a lead's event is led by them) |
| GET / PATCH | `/events/{id}` | View: club members. Edit: president or the event's lead. Only the president changes the lead. |
| POST | `/events/{id}/status` | President or lead. draft→published/cancelled; published→draft/cancelled/completed |
| GET | `/events/{id}/readiness` | President or lead (Event Autopilot) |
| GET | `/events/{id}/schedule-check` | President or lead (SyncSphere) |
| GET / POST | `/events/{id}/tasks` | View: club members. Create: president or lead |
| GET | `/tasks/mine` | Anyone: own tasks on open events |
| PATCH | `/tasks/{id}` | President or lead: edit, assign, `unassign: true` |
| POST | `/tasks/{id}/status` | Assignee or organizers. `blocked` needs `blocker_note` |
| GET | `/tasks/{id}/activity` | Club members who can see the event |

Rules enforced on the server:
- `club_id` always comes from the signed-in user. Request bodies that include `club_id`, `status` or other unknown fields are rejected (422).
- Other clubs' events and tasks answer 404.
- Assignees and leads must belong to the club.
- Starts and deadlines must be in the future. Events must end after they start and last at most 72 hours.
- Publishing needs a start, end, venue and lead.
- Cancelled and completed events are frozen.
- Duplicates are blocked by unique indexes: the same task title twice on one event, or the same event name at the same start time.
- Repeated status clicks do nothing.
- Times are stored in UTC. Inputs without an offset are read as IST; pages display IST.

## Readiness rules (Event Autopilot)

The score starts at 100. Each rule that fires subtracts points, capped per rule:

| Rule | Points |
|---|---|
| No lead / no venue / no start-end / description under 20 chars | −15 / −10 / −10 / −5 |
| Still a draft with ≤ 7 days to go | −10 |
| No tasks with ≤ 14 days to go | −20 |
| Critical task not done with ≤ 48 h to go | −15 each (max −30) |
| Overdue task | −8 each (max −24) |
| Blocked task | −10 each (max −20) |
| Unassigned task | −5 each (max −15) |
| < 70% of tasks done with ≤ 72 h to go | −10 |
| Venue clash / lead clash / time overlap (SyncSphere) | −20 / −10 / −5 each (max −10) |

Bands: 80 or more is on track, 50–79 needs attention, below 50 at risk. Any high-severity issue
(such as a blocked task) rules out "on track". Past, cancelled and completed events are not scored.

## SyncSphere rules

- Events overlap when `a.start < b.end and b.start < a.end`, so back-to-back events don't clash.
- Cancelled events are ignored.
- Venues are compared after trimming, lower-casing and collapsing spaces.
- Only the club's own events are compared. Nothing claims a venue is free campus-wide.
- An event without times is reported as "could not be checked", never as free.
- Alternative slots keep the same length. They start on the half hour between 09:00 and 21:00 IST, within the next 7 days, in the future, and at least 30 minutes clear of other events.

## Files

Member 2 only (new files):

```
migrations/010_events.sql, migrations/011_crewpulse.sql
app/models/crewpulse.py
app/schemas/events.py, app/schemas/crewpulse.py
app/services/event_service.py, task_service.py, autopilot.py, syncsphere.py
app/api/v1/events.py, app/api/v1/tasks.py
app/web/event_pages.py, app/templates/events/*.html, app/static/events.css, app/static/events.js
scripts/seed_events_demo.py
tests/test_events.py, test_tasks.py, test_autopilot.py, test_syncsphere.py, test_event_pages.py
```

Shared files changed (small, additive, **need team approval**):

| File | Change |
|---|---|
| `app/models/shared.py` | `Event` gains `description, starts_at, ends_at, venue, status, created_by, updated_at` (no renames) |
| `app/api/v1/__init__.py` | Registers the `events` and `tasks` routers |
| `app/main.py` | Registers `event_pages.router` |
| `app/templates/base.html` | "Events" and "My tasks" nav links |

## Notes for the team

- `events` is extended in place by `010_events.sql`. Ids, `name`, `event_date` and `lead_id` are unchanged, and `event_date` is kept equal to the start date in IST.
- Rows that existed before the migration become `published`. New rows default to `draft`, so other modules that create events should set `status` themselves.
- Proposed migration numbering: Member 2 uses `010–019`, Member 1 `020–029`, Member 4 `030+`.
- Member 1: ticket prices, capacity and attendance belong in your own tables, keyed by `events.id`. Please don't add them as columns on `events`.
- Member 3: `seed_finance_demo.py` crashes printing `₹` on Windows consoles (the data is already saved by then). Set `PYTHONIOENCODING=utf-8` as a workaround.
- Event audit entries (`event_created`, `event_updated`, `event_status_changed`) reuse Member 3's `audit_logs` table through `audit_service.record`.

---

# FairSeat event side (Member 2) — ticket configuration, discovery, lifecycle protection

**Status: joined to the reservation service, pending team approval.** The migrations are in
`migrations/pending/` and are applied only to the disposable local test database. Nothing has been applied to
any shared database. Payments, refunds and QR credentials are not part of this: Member 3 owns reservations and
payments, and Ameer owns MemberCore pricing and the FairSeat/GateSync UI.

What changed when the two sides were joined (branch `fairseat-event-integration`):

- There is **one category table**, `ticket_categories` (from `020_fairseat.sql`). The `event_ticket_categories`
  table first proposed here is not created, and `012_event_tickets.sql` is not used.
- `ticket_inventory` returns **real seat counts**: confirmed seats plus unexpired holds.
- Cancelling an event releases unpaid holds and free bookings in the same transaction, and is refused while any
  booking is paid.
- At most **6 tickets per booking**; category names are **1 to 120 characters**.
- Everything here answers 404, and the guards do nothing, unless `FAIRSEAT_ENABLED=1`.

## Decisions taken

| Topic | Decision |
|---|---|
| Capacity | **Strict partition.** `event_ticket_settings.capacity` is split exactly across categories. A category can never exceed the event, and the categories together can never exceed it. A smaller total is allowed only while sales are off, and is shown as a warning. |
| Hidden category | Paused, not deleted. It keeps its seats and its id. |
| Member pricing | Not computed here. `member_discount_eligible` per category says whether MemberCore's discount applies; MemberCore stays the only discount engine. |
| Money | Integer paise, 0 – 10,000,000 (₹1,00,000) per ticket, INR only. Strict integers: `"100"`, `99.5` and `true` are rejected. |
| Booking window | `opens_at <= now < closes_at` (open inclusive, close exclusive, the same as `booking-rules.ts`), and `closes_at <= event.starts_at`. Bookings only on **published** events with `sales_enabled`. |
| Discovery read policy | Signed-in members of the event's own club only (no anonymous or public reads until a policy is approved). Only published events with sales on, not yet ended, with at least one visible category. Drafts are never listed, not even for organizers. |
| Who configures | `can_manage`: the club president, or the event's lead. Others get 403; other clubs and drafts they can't see get 404. |
| Lifecycle | Locks **fail closed** while booked seats are unknown (see below). |

## Schema (`migrations/pending/020_fairseat.sql`, then `021_fairseat_event_settings.sql`; additive)

```sql
event_ticket_settings (                       -- one row per ticketed event (021)
  event_id uuid PK, club_id uuid NOT NULL,
  FOREIGN KEY (event_id, club_id) REFERENCES events (id, club_id) ON DELETE CASCADE,
  sales_enabled boolean NOT NULL DEFAULT false,
  capacity integer NOT NULL CHECK (1..100000),
  opens_at timestamptz NOT NULL, closes_at timestamptz NOT NULL CHECK (closes_at > opens_at),
  max_per_booking integer NOT NULL DEFAULT 6 CHECK (1..6),
  sales_activated_at timestamptz,             -- first time sales were live on a published event; never cleared
  created_by, updated_by uuid NOT NULL, created_at, updated_at timestamptz)

ticket_categories (                           -- the one category table (020, extended by 021)
  id uuid PK,
  event_id uuid REFERENCES events (id), REFERENCES event_ticket_settings (event_id) ON DELETE RESTRICT,
  name text CHECK (1..120 chars), UNIQUE (event_id, lower(btrim(name))),
  description text (<= 300),
  base_price_paise bigint CHECK (0..10000000),    -- "price_paise" in this API
  pricing_version integer,                        -- bumped when price or discount eligibility changes
  capacity integer CHECK (> 0),
  sales_open_at, sales_close_at timestamptz NULL, -- optional narrower window; NULL = the event's
  active boolean DEFAULT true,                    -- "is_visible" in this API
  member_discount_eligible boolean DEFAULT true, sort_order smallint,
  created_by uuid REFERENCES users (id))
```

- `events_id_club_key` is a unique index on `events (id, club_id)`. It lets the composite foreign key prove that the settings row's `club_id` equals the event's club.
- Deferred constraint triggers on both tables enforce the partition as a backstop to the service checks.
- Existing events get no row, which means "not configured". No existing row or column of `events` changes.
- Bookings, quotes and per-seat ticket rows reference `ticket_categories` with `ON DELETE RESTRICT` (see `FAIRSEAT.md`).

## API (all under `/api/v1`, signed-in only)

| Method | Path | Who | Notes |
|---|---|---|---|
| GET | `/events/{id}/tickets` | Organizers | Config, `sale_state`, `locked`, `warnings`, `seats_committed` (confirmed seats plus unexpired holds), and `seats_committed` per category |
| PUT | `/events/{id}/tickets` | Organizers | Replaces the whole configuration. Send the category `id` to keep a category; leaving an existing id out removes that category. |
| GET | `/discovery/events` | Club members | Published + on-sale events of the caller's club, soonest first |
| GET | `/discovery/events/{id}` | Club members | 404 for drafts, other clubs, sales off |

Page: `/events/{id}/tickets` (organizers only). The event page also shows a "FairSeat · tickets" card to organizers.

PUT body:

```json
{"sales_enabled": true, "capacity": 150, "opens_at": "2026-10-12T10:00", "closes_at": "2026-10-20T18:00:00+05:30",
 "max_per_booking": 4,
 "categories": [{"id": "<existing uuid, optional>", "name": "General", "price_paise": 25000, "capacity": 120,
                 "member_discount_eligible": true, "is_visible": true, "description": null}]}
```

Times without an offset are read as IST. Responses are always UTC. Errors use the shared envelope
`{"error": {"code", "message", "details": [{"code", "message"}, ...]}}`. The codes are `category_over_capacity`,
`over_allocated`, `unallocated_seats`, `no_categories`, `window_after_start`, `missing_schedule`, `event_closed`,
`unknown_category`, `duplicate_category` (400/409), the lock codes below (409), and 422 for invalid input.

Discovery item (what FairSeat consumes):

```json
{"id": "uuid", "club_id": "uuid", "name": "Bake Sale", "description": "...", "venue": "Campus Quad",
 "starts_at": "2026-10-17T04:30:00Z", "ends_at": "2026-10-17T10:30:00Z", "status": "published",
 "booking_opens_at": "...Z", "booking_closes_at": "...Z", "sale_state": "not_open | open | closed",
 "availability": "unavailable",
 "categories": [{"id": "uuid", "name": "General", "description": null, "price_paise": 25000, "currency": "INR",
                 "member_discount_eligible": true, "capacity": 120, "max_per_booking": 4, "seats_remaining": null}]}
```

`capacity` is the configured total. `seats_remaining` stays `null` and `availability` stays `"unavailable"` until
the reservation service reports real counts. Nothing is estimated.

## Lifecycle protection

`locked` = `sales_activated_at` is set **and** `now >= opens_at`, meaning bookings may exist. Booked seats come
from `app/services/ticket_inventory.committed_seats()`: confirmed seats plus unexpired holds, per category.

| Change | With seats booked | With none |
|---|---|---|
| Event to `draft` | 409 `ticket_sales_started` | allowed |
| Event to `cancelled`, only unpaid holds and free bookings | allowed: they are released and free tickets revoked in the same transaction | allowed |
| Event to `cancelled`, any paid booking | 409 `cancellation_requires_refunds`; nothing is released | — |
| Event to `completed` | allowed | allowed |
| Change event start/end | 409 `ticket_sales_started` | allowed |
| Venue, description, lead | allowed | allowed |
| Reduce capacity / category seats (once locked) | allowed down to the seats taken; below that 409 `capacity_locked` / `category_locked` | allowed |
| Remove a category | 409 `category_locked` if it has ever been quoted or booked (hide it instead) | allowed |
| Move `opens_at` (once locked) | 409 `window_locked` | 409 `window_locked` |
| Add seats, change price (future buyers), hide, pause sales, move `closes_at` (no later than start) | allowed | allowed |

Even before sales open, rescheduling an event so it starts before `closes_at` is refused
(`booking_window_conflict`). Nothing is silently clamped. Publishing an event with sales on re-checks the
configuration (`ticket_config_invalid`) and sets `sales_activated_at`. If the event was changed by someone else
between being read and being locked, the change is refused with 409 `event_changed`.

## How the reservation side connects

1. **Lock order.** Event row, then `event_ticket_settings` row, then `ticket_categories` rows in id order, then
   booking rows. Booking, cancelling, expiring holds, saving ticket settings and status or schedule changes all take
   their locks through `app/services/ticket_locks.py`.
2. **Booking re-checks, under those locks:** event `published` with a start time, `sales_enabled`, the event window
   and the category's own window if it has one, `now < starts_at`, category visible, the per-booking and per-person
   limits, the category's seats and the event's total.
3. **One category writer.** `ticket_service.save_config`. `PUT /events/{id}/tickets`, the organizer page and
   `POST`/`PATCH /ticket-categories` all end there.
4. **Cancellation.** Paid bookings block it until the verified refund workflow exists. Event Management never
   changes payment state.

## For Ameer (FairSeat UI)

Map `DiscoveryEventOut` to `EventView` in the server adapter (`server-integration.ts`): `eventId` from `id`,
`title` from `name`, `startsAt`/`endsAt` via `new Date(x).toISOString()` (the backend sends `...Z` without
milliseconds, while `parseEvents` requires `.sssZ`), `saleState` from `sale_state` (`not_open` maps to `closed`),
`categoryId` from `id`, `basePricePaise` from `price_paise`, `maxQuantity` from `max_per_booking`, and
`availability` from `seats_remaining` (now a real number; `availability` is `"live"`).

**Required change:** `parseEvents` rejects the whole listing if any `description` is null or empty. Events may
have no description, so map `null` to a placeholder or relax the check.

MemberCore pricing input: `basePricePaise = price_paise`, `eventClubId = club_id`. Skip the discount when
`member_discount_eligible` is false. Authentication is unchanged: FastAPI still verifies its own JWT
(`sub` = `users.id`). The Supabase identity mapping is still an open team decision.

## Files (this change)

New: `migrations/pending/021_fairseat_event_settings.sql` (in place of the first proposal's `012`), `app/models/tickets.py`, `app/schemas/tickets.py`,
`app/services/ticket_rules.py`, `app/services/ticket_service.py`, `app/services/ticket_inventory.py`,
`app/api/v1/event_tickets.py`, `app/templates/events/tickets.html`, `app/static/tickets.js`,
`tests/test_event_tickets.py`.

Changed: `app/services/event_service.py` (two guard calls), `app/web/event_pages.py` (page, paise filters),
`app/templates/events/detail.html` (organizer card), `app/static/events.css`, and `app/api/v1/__init__.py`
(router registration; this is a shared file).
