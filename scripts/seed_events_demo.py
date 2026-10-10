"""Add a realistic semester of events and volunteer tasks to the demo club.

    python -m scripts.seed_finance_demo --reset   # first: people, events, money (Member 3)
    python -m scripts.seed_events_demo            # then: schedules, tasks, a clash (Member 2)

Never drops or truncates anything. Refuses to run twice. Dates are relative to
today, so the demo always shows upcoming events. Writes go through the same
services as the API; the only direct edits are back-dating (past events, one
overdue deadline), which the API rightly refuses to do.
"""

import asyncio
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import select

from app.core.dependencies import CurrentUser
from app.db.database import SessionLocal
from app.models.registration import Venue
from app.models.shared import Club, Event, User
from app.schemas.crewpulse import TaskCreate, TaskStatusChange
from app.schemas.events import EventCreate, EventUpdate
from app.services import event_service, task_service
from app.services.syncsphere import IST


def acting(user: User) -> CurrentUser:
    return CurrentUser(id=user.id, club_id=user.club_id, role=user.role, is_active_member=user.is_active_member,
                       full_name=user.full_name, email=user.email)


def ist_at(days: int, hour: int, minute: int = 0) -> datetime:
    day = datetime.now(IST).date() + timedelta(days=days)
    return datetime.combine(day, time(hour, minute), IST)


async def seed(session) -> None:
    club = await session.scalar(select(Club).order_by(Club.created_at).limit(1))
    if club is None:
        raise SystemExit("No club found. Run `python -m scripts.seed_finance_demo --reset` first.")
    if await session.scalar(select(Event).where(Event.club_id == club.id, Event.name == "Spring Gala")):
        raise SystemExit("Event demo data is already loaded.")

    people = {u.full_name: u for u in await session.scalars(select(User).where(User.club_id == club.id))}
    president = next(u for u in people.values() if u.role == "president")
    sneha, karthik = people["Sneha Iyer"], people["Karthik Nair"]
    aditi, varun, meera, arjun, nisha = (people[n] for n in
                                         ("Aditi Sharma", "Varun Reddy", "Meera Pillai", "Arjun Das", "Nisha Kapoor"))
    p, s, k = acting(president), acting(sneha), acting(karthik)
    events = {e.name: e for e in await session.scalars(select(Event).where(Event.club_id == club.id))}

    # Venues from the college list (migration 012), for the events students can register for.
    venues = {v.name: v.id for v in await session.scalars(select(Venue))}
    lawn, mini = venues["Central Lawn"], venues["Mini Auditorium"]

    # --- Past events from the finance demo get real times and are completed ----------
    for name, hours in (("Freshers' Night", (18, 22)), ("Tech Fest Workshop", (10, 16))):
        if event := events.get(name):
            day = event.event_date
            event.starts_at = datetime.combine(day, time(hours[0]), IST)
            event.ends_at = datetime.combine(day, time(hours[1]), IST)
            event.venue = "Main Hall" if name == "Freshers' Night" else "CS Lab 2"
            event.status = "completed"

    # --- Bake Sale: the fundraiser, a week away ----------------------------------------
    bake_sale = events.get("Bake Sale")
    if bake_sale:
        await event_service.update_event(session, s, bake_sale, EventUpdate(
            starts_at=ist_at(7, 10), ends_at=ist_at(7, 16), venue_id=lawn,
            registration_mode="free", max_participants=300,   # open to every student, no charge
            description="Fundraiser bake sale. Every rupee goes to the spring gala budget."))
        await event_service.change_status(session, s, bake_sale, "published")
        sale_tasks = [
            ("Bake 60 brownies", aditi, "high", True, ist_at(6, 20)),
            ("Buy supplies", varun, "high", True, ist_at(5, 18)),
            ("Run the table on the day", meera, "medium", True, ist_at(7, 9, 30)),
            ("Make price cards", None, "low", False, ist_at(6, 12)),
        ]
        created = [await task_service.create_task(session, s, bake_sale, TaskCreate(
            title=t, assignee_id=a.id if a else None, priority=pr, is_critical=c, due_at=d))
            for t, a, pr, c, d in sale_tasks]
        await task_service.set_status(session, acting(aditi), created[0].id, TaskStatusChange(status="done"))
        await task_service.set_status(session, acting(varun), created[1].id, TaskStatusChange(status="in_progress"))
        created[1].due_at = datetime.now(timezone.utc) - timedelta(days=1)  # back-dated: shows an overdue task

    # --- Spring Gala: the big one, with a blocker ----------------------------------------
    gala = await event_service.create_event(session, p, EventCreate(
        name="Spring Gala", starts_at=ist_at(12, 18), ends_at=ist_at(12, 22), venue_id=mini, lead_id=sneha.id,
        registration_mode="paid", price_paise=25000, max_participants=250,   # ₹250 a head, paid online
        description="The association's biggest night: dinner, live music and the annual awards."))
    await event_service.change_status(session, p, gala, "published")
    gala_tasks = [
        ("Book the DJ", arjun, "high", True, ist_at(4, 18)),
        ("Confirm catering", nisha, "high", True, ist_at(8, 18)),
        ("Decorate the hall", meera, "medium", False, ist_at(12, 15)),
        ("Print awards certificates", aditi, "medium", False, ist_at(10, 18)),
        ("Ticket desk volunteers", None, "medium", True, ist_at(11, 18)),
        ("Sound check", None, "high", True, ist_at(12, 16)),
    ]
    created = [await task_service.create_task(session, s, gala, TaskCreate(
        title=t, assignee_id=a.id if a else None, priority=pr, is_critical=c, due_at=d))
        for t, a, pr, c, d in gala_tasks]
    await task_service.set_status(session, acting(arjun), created[0].id, TaskStatusChange(
        status="blocked", blocker_note="DJ wants a ₹5,000 advance before confirming."))
    await task_service.set_status(session, acting(nisha), created[1].id, TaskStatusChange(status="in_progress"))
    await task_service.set_status(session, acting(aditi), created[3].id, TaskStatusChange(status="done"))

    # --- A draft that clashes with the gala, for SyncSphere ------------------------------
    meetup = await event_service.create_event(session, k, EventCreate(
        name="Hackathon Prep Meetup", starts_at=ist_at(12, 20), ends_at=ist_at(12, 22), venue_id=mini))   # same hall as the gala
    await task_service.create_task(session, k, meetup, TaskCreate(title="Reserve projector", due_at=ist_at(10, 12)))

    # --- A quiet, healthy event --------------------------------------------------------
    talk = await event_service.create_event(session, k, EventCreate(
        name="Alumni Career Talk", starts_at=ist_at(20, 17), ends_at=ist_at(20, 19), venue="Seminar Room 1",
        description="Two alumni from product companies talk about internships and placements."))
    await event_service.change_status(session, k, talk, "published")
    for title, who in (("Invite speakers", varun), ("Book seminar room", karthik)):
        task = await task_service.create_task(session, k, talk, TaskCreate(title=title, assignee_id=who.id))
        await task_service.set_status(session, acting(who), task.id, TaskStatusChange(status="done"))

    await session.commit()
    print("Event demo data loaded:")
    print("  Event lead  Sneha Iyer   (Spring Gala, Bake Sale)")
    print("  Event lead  Karthik Nair (Hackathon Prep Meetup clashes with the gala)")
    print("  Volunteer   Arjun Das    (blocked: Book the DJ)")
    print("  President   " + president.full_name)


async def main() -> None:
    async with SessionLocal() as session:
        await seed(session)


if __name__ == "__main__":
    asyncio.run(main())
