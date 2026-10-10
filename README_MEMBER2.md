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
