"""Add demo data for the optional modules to the seeded club.

    python -m scripts.migrate_pending       # first: the modules' tables (local database only)
    python -m scripts.seed_modules_demo     # then: plans, tickets on sale, announcements, notes

Never drops or truncates anything. Refuses to run twice. Writes go through the same
services as the API. Nobody is made a member and nothing is paid: joining, paying, booking
and checking in are left for the demo itself.
"""

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.dependencies import CurrentUser
from app.db.database import SessionLocal
from app.models.membercore import MembershipPlan
from app.models.shared import Club, Event, User
from app.schemas.community import AnnouncementIn, HandoverNoteIn
from app.schemas.membercore import PlanIn
from app.schemas.tickets import TicketCategoryIn, TicketConfigIn
from app.services import announcement_service, handover_service, membership_service, modules, ticket_service


def acting(user: User) -> CurrentUser:
    return CurrentUser(id=user.id, club_id=user.club_id, role=user.role, is_active_member=user.is_active_member,
                       full_name=user.full_name, email=user.email)


async def seed(session) -> None:
    found = await modules.load(session)
    missing = {"memberships", "announcements", "handover"} - found
    if missing:
        raise SystemExit("Run `python -m scripts.migrate_pending` first (missing: " + ", ".join(sorted(missing)) + ").")
    club = await session.scalar(select(Club).order_by(Club.created_at).limit(1))
    if club is None:
        raise SystemExit("No club found. Run `python -m scripts.seed_finance_demo --reset` first.")
    if await session.scalar(select(MembershipPlan.id).where(MembershipPlan.club_id == club.id).limit(1)):
        raise SystemExit("Module demo data is already loaded.")
    people = list(await session.scalars(select(User).where(User.club_id == club.id)))
    president = acting(next(u for u in people if u.role == "president"))
    treasurer = acting(next(u for u in people if u.role == "treasurer"))

    await membership_service.create_plan(session, treasurer, PlanIn(
        name="Annual member", description="A year of member prices on tickets and merchandise.",
        price_paise=50000, validity_days=365, ticket_discount_bps=2000))
    await membership_service.create_plan(session, treasurer, PlanIn(
        name="Semester member", description="One semester.", price_paise=30000, validity_days=180,
        ticket_discount_bps=1000))
    await membership_service.create_plan(session, treasurer, PlanIn(
        name="Honorary", description="For alumni and guests. Approved by the president.", price_paise=0,
        validity_days=365, ticket_discount_bps=1000))

    now = datetime.now(timezone.utc)
    upcoming = list(await session.scalars(
        select(Event).where(Event.club_id == club.id, Event.status == "published", Event.starts_at > now + timedelta(days=1),
                            Event.ends_at.isnot(None))
        .order_by(Event.starts_at).limit(2)
    ))
    ticketed = []
    if "tickets" in found or modules.migrated("tickets"):
        for event, (general, vip) in zip(upcoming, ((15000, 40000), (0, 25000))):
            if await ticket_service.get_settings_row(session, event.id) is not None:
                continue
            await ticket_service.save_config(session, president, event, TicketConfigIn(
                sales_enabled=True, capacity=60, opens_at=now - timedelta(hours=1),
                closes_at=event.starts_at - timedelta(hours=2),
                categories=[
                    TicketCategoryIn(name="General", price_paise=general, capacity=50),
                    TicketCategoryIn(name="Front rows", description="Closest to the stage.", price_paise=vip, capacity=10),
                ]))
            ticketed.append(event.name)

    await announcement_service.post(session, president, AnnouncementIn(
        title="Memberships are open", pinned=True,
        body="Join from the Memberships page. Members get lower ticket prices from the moment their dues are paid."))
    await announcement_service.post(session, treasurer, AnnouncementIn(
        title="Door rota for this month", audience="volunteers",
        body="Volunteers on the door: open Check-in on your phone and pick the event. Scan each ticket once."))
    if upcoming:
        await announcement_service.post(session, president, AnnouncementIn(
            title=f"{upcoming[0].name}: what to bring", audience="event", event_id=upcoming[0].id,
            body="Bring your ticket QR code (My tickets) and your college ID. Doors open 30 minutes before the start."))

    await handover_service.create(session, treasurer, HandoverNoteIn(
        role="treasurer", title="Closing the books each month",
        body="1. Export the ledger for the month.\n2. Match it to the bank statement.\n3. Chase unpaid reimbursements.\n"
             "4. Send the summary to the president by the 5th."))
    await handover_service.create(session, president, HandoverNoteIn(
        role="president", title="Who to call at the college",
        body="Venue bookings go through the Student Affairs office, two weeks ahead. Budget approval needs the "
             "faculty advisor's signature before any spend over the auto-approval limit."))
    await handover_service.create(session, president, HandoverNoteIn(
        role="everyone", title="How the club runs", body="Events are planned on the Events page. Anyone can volunteer "
        "for a task. Expenses are claimed with a receipt photo and approved by two people."))
    await session.commit()
    print("Loaded: 3 membership plans, 3 announcements, 3 handover notes"
          + (f", tickets on sale for {' and '.join(ticketed)}" if ticketed else ", no tickets (no upcoming published event)"))


async def main() -> None:
    async with SessionLocal() as session:
        await seed(session)


if __name__ == "__main__":
    asyncio.run(main())
