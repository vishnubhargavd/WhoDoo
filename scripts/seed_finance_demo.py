"""Fill the database with a realistic club for the finance demo.

    python -m scripts.seed_finance_demo --reset

`--reset` drops and recreates every table first, so it only runs against a local
database unless you also pass `--force`. Everything is created through the same
services the API uses, so the seeded ledger, stock and approval trails are
consistent with each other. Payments always use the mock provider here.
"""

import argparse
import asyncio
import io
import os
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

os.environ["PAYMENT_PROVIDER"] = "mock"

from sqlalchemy import func, select  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.dependencies import CurrentUser  # noqa: E402
from app.db.database import SessionLocal, asyncpg_dsn  # noqa: E402
from app.db.migrate import run_migrations  # noqa: E402
from app.models.shared import Club, Event, User  # noqa: E402
from app.schemas.expense import ExpenseClaimCreate  # noqa: E402
from app.schemas.merch import OrderCreate, OrderItemIn, ProductCreate, VariantCreate  # noqa: E402
from app.schemas.payment import VerifyPayment  # noqa: E402
from app.services import expense_service, ledger_service, merch_service, payment_service  # noqa: E402
from scripts.make_sample_receipts import draw  # noqa: E402

LOCAL_HOSTS = ("@localhost", "@127.0.0.1", "//localhost", "//127.0.0.1")


def at(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 12, 0, tzinfo=timezone.utc)


def acting(user: User) -> CurrentUser:
    return CurrentUser(
        id=user.id, club_id=user.club_id, role=user.role, is_active_member=user.is_active_member,
        full_name=user.full_name, email=user.email,
    )


def receipt_png(vendor: str, when: date, item: str, amount: Decimal) -> bytes:
    lines = [
        (vendor.upper(), "title"),
        ("Bengaluru, Karnataka", "small"),
        ("", "small"),
        (f"Bill No: {uuid.uuid4().int % 9000 + 1000}        Date: {when:%d/%m/%Y}", "body"),
        ("----------------------------------------", "body"),
        (f"{item:<28}{amount:>10.2f}", "body"),
        ("----------------------------------------", "body"),
        (f"TOTAL                  Rs. {amount:.2f}", "title"),
    ]
    buffer = io.BytesIO()
    draw(lines).save(buffer, format="PNG")
    return buffer.getvalue()


async def seed(session) -> None:
    # --- People and events (placeholder tables) --------------------------------------
    club = Club(id=uuid.uuid4(), name="WhoDoo Student Club")
    session.add(club)
    await session.flush()

    def person(name: str, role: str, member: bool = True) -> User:
        user = User(
            id=uuid.uuid4(), club_id=club.id, full_name=name, role=role, is_active_member=member,
            email=f"{name.lower().replace(' ', '.')}@whodoo.example",
        )
        session.add(user)
        return user

    president = person("Ananya Rao", "president")
    treasurer = person("Rohan Mehta", "treasurer")
    lead_events = person("Sneha Iyer", "event_lead")
    lead_tech = person("Karthik Nair", "event_lead")
    volunteers = [person(name, "volunteer") for name in
                  ("Aditi Sharma", "Varun Reddy", "Meera Pillai", "Arjun Das", "Nisha Kapoor")]
    members = [person(name, "member", member=index < 8) for index, name in enumerate(
        ("Ishaan Gupta", "Priya Menon", "Rahul Verma", "Divya Joshi", "Sameer Khan",
         "Tanvi Desai", "Nikhil Bose", "Pooja Hegde", "Aman Sethi", "Lakshmi Narayan"))]
    await session.flush()

    freshers = Event(id=uuid.uuid4(), club_id=club.id, name="Freshers' Night", event_date=date(2026, 8, 22), lead_id=lead_events.id)
    tech_fest = Event(id=uuid.uuid4(), club_id=club.id, name="Tech Fest Workshop", event_date=date(2026, 9, 19), lead_id=lead_tech.id)
    bake_sale = Event(id=uuid.uuid4(), club_id=club.id, name="Bake Sale", event_date=date(2026, 10, 17), lead_id=lead_events.id)
    session.add_all([freshers, tech_fest, bake_sale])
    await session.flush()

    # --- Income recorded by other modules, via the ledger service contract ------------
    books = dict(club_id=club.id, created_by=treasurer.id)
    for index, member in enumerate(members[:8] + volunteers + [lead_events, lead_tech]):
        await ledger_service.record_income(
            session, source_module="membership", amount=Decimal("500"), payer_id=member.id,
            category="dues", description=f"Annual dues: {member.full_name}",
            created_at=at(2026, 8, 3 + index % 10), **books,
        )
    for index in range(40):  # Freshers' Night sold out: 40 tickets at ₹200
        await ledger_service.record_income(
            session, source_module="event_ticket", amount=Decimal("200"), event_id=freshers.id,
            reference_type="ticket", category="tickets", description="Freshers' Night ticket",
            created_at=at(2026, 8, 10 + index % 12), **books,
        )
    for index in range(12):  # Tech Fest Workshop under-sold: 12 tickets at ₹150
        await ledger_service.record_income(
            session, source_module="event_ticket", amount=Decimal("150"), event_id=tech_fest.id,
            reference_type="ticket", category="tickets", description="Tech Fest Workshop ticket",
            created_at=at(2026, 9, 5 + index), **books,
        )
    await ledger_service.record_income(
        session, source_module="fundraiser", amount=Decimal("2500"), category="sponsorship",
        description="Sponsorship from Campus Cafe", created_at=at(2026, 9, 1), **books,
    )

    # --- Direct expenses paid by the treasurer ---------------------------------------
    for event, amount, category, text, when in (
        (freshers, "3000", "other", "Hall booking for Freshers' Night", at(2026, 8, 12)),
        (tech_fest, "4500", "other", "Projector and lab equipment rental", at(2026, 9, 15)),
        (None, "350", "printing", "Membership cards", at(2026, 8, 5)),
    ):
        await ledger_service.record_expense(
            session, entry_type="expense", source_module="manual", amount=Decimal(amount), category=category,
            description=text, event_id=event.id if event else None, approved_by=treasurer.id,
            payment_status="paid", created_at=when, **books,
        )

    # --- Expense claims in every state ---------------------------------------------------
    async def claim(who: User, event: Event, title: str, vendor: str, category: str, amount: str, when: date):
        value = Decimal(amount)
        upload = await expense_service.upload_receipt(session, acting(who), receipt_png(vendor, when, title, value))
        return await expense_service.create_claim(
            session, acting(who),
            ExpenseClaimCreate(
                receipt_id=upload.receipt_id, title=title, description=f"{title} for {event.name}",
                category=category, amount=value, expense_date=when, vendor_name=vendor, event_id=event.id,
            ),
        )

    aditi, varun, meera, arjun, nisha = volunteers
    t, p, events_lead, tech_lead = acting(treasurer), acting(president), acting(lead_events), acting(lead_tech)

    paid_food = await claim(aditi, freshers, "Snacks and drinks", "Fresh Basket Caterers", "food", "1200", date(2026, 8, 21))
    await expense_service.approve_claim(session, events_lead, paid_food.id)
    await expense_service.approve_claim(session, t, paid_food.id, "Within the event budget")
    await expense_service.mark_paid(session, t, paid_food.id)

    paid_print = await claim(varun, tech_fest, "Workshop handouts", "Quick Print Hub", "printing", "650", date(2026, 9, 17))
    await expense_service.approve_claim(session, tech_lead, paid_print.id)
    await expense_service.mark_paid(session, t, paid_print.id)

    approved = await claim(meera, tech_fest, "Speaker cab fare", "City Cabs", "travel", "480", date(2026, 9, 19))
    await expense_service.approve_claim(session, tech_lead, approved.id)

    awaiting_final = await claim(arjun, freshers, "Stage decoration", "Festive Decor House", "decoration", "2500", date(2026, 8, 20))
    await expense_service.approve_claim(session, events_lead, awaiting_final.id)

    await claim(nisha, bake_sale, "Paper plates and napkins", "Daily Needs Store", "supplies", "320", date(2026, 10, 6))
    await claim(varun, tech_fest, "Extension boards and cables", "Volt Electricals", "other", "1800", date(2026, 9, 18))

    rejected = await claim(arjun, freshers, "Dinner after the event", "Spice Garden", "food", "900", date(2026, 8, 22))
    await expense_service.reject_claim(session, events_lead, rejected.id, "Personal meal, not a club expense")

    sent_back = await claim(meera, bake_sale, "Bake sale posters", "Quick Print Hub", "printing", "260", date(2026, 10, 5))
    await expense_service.request_changes(session, events_lead, sent_back.id, "Please attach the itemised bill")

    # --- Merchandise ---------------------------------------------------------------------
    def variant(size: str, color: str, sku: str, stock: int, override: str | None = None) -> VariantCreate:
        return VariantCreate(size=size, color=color, sku=sku, stock_quantity=stock,
                             price_override=Decimal(override) if override else None)

    hoodie = await merch_service.create_product(session, t, ProductCreate(
        name="Club Hoodie", description="Heavyweight fleece hoodie with the club crest.",
        base_price=Decimal("999"), member_price=Decimal("799"),
        variants=[variant("S", "Black", "HOOD-BLK-S", 6), variant("M", "Black", "HOOD-BLK-M", 10),
                  variant("L", "Black", "HOOD-BLK-L", 8), variant("XL", "Black", "HOOD-BLK-XL", 4, "1099")],
    ))
    tee = await merch_service.create_product(session, t, ProductCreate(
        name="Club T-Shirt", description="Cotton tee, screen-printed logo.",
        base_price=Decimal("499"), member_price=Decimal("399"),
        variants=[variant("M", "White", "TEE-WHT-M", 15), variant("L", "White", "TEE-WHT-L", 12)],
    ))
    mug = await merch_service.create_product(session, t, ProductCreate(
        name="Enamel Mug", description="Campfire-style mug.",
        base_price=Decimal("299"), member_price=Decimal("249"),
        variants=[variant("Standard", "White", "MUG-WHT", 20)],
    ))
    sku = {v.sku: v.id for product in (hoodie, tee, mug) for v in product.variants}
    provider = payment_service.get_provider()

    async def order(buyer: User, lines: list[tuple[str, int]], outcome: str = "paid", event: Event | None = None):
        placed = await merch_service.create_order(session, acting(buyer), OrderCreate(
            items=[OrderItemIn(variant_id=sku[code], quantity=quantity) for code, quantity in lines],
            event_id=event.id if event else None,
        ))
        if outcome == "unpaid":
            return placed
        payment = await payment_service.create_payment_order(session, acting(buyer), placed.id)
        simulated = provider.simulate_payment(payment.razorpay_order_id, succeed=outcome != "failed")
        await payment_service.verify_payment(session, acting(buyer), VerifyPayment(**simulated))
        for step in {"packed": ["packed"], "delivered": ["packed", "delivered"], "refunded": ["cancelled"]}.get(outcome, []):
            await merch_service.update_status(session, t, placed.id, step)
        return placed

    # The Hoodie in M is left untouched at 10, ready for the live demo purchase.
    await order(members[0], [("HOOD-BLK-L", 1)], "delivered")
    await order(members[1], [("TEE-WHT-M", 2)], "delivered")
    await order(members[2], [("MUG-WHT", 1), ("TEE-WHT-L", 1)], "packed")
    await order(members[8], [("HOOD-BLK-S", 1)], "packed")            # not an active member: pays ₹999
    await order(members[3], [("TEE-WHT-M", 1)], "paid", event=freshers)
    await order(volunteers[0], [("MUG-WHT", 2)], "paid", event=freshers)
    await order(members[4], [("HOOD-BLK-XL", 1)], "paid")
    await order(members[5], [("TEE-WHT-L", 1)], "refunded")
    await order(members[6], [("MUG-WHT", 1)], "failed")
    await order(members[9], [("HOOD-BLK-L", 1)], "unpaid")

    await session.commit()

    # --- Report ---------------------------------------------------------------------------
    summary = await ledger_service.get_summary(session, club.id)
    print(f"\nSeeded {club.name}")
    print(f"  People: {await session.scalar(select(func.count()).select_from(User))} "
          "(1 president, 1 treasurer, 2 event leads, 5 volunteers, 10 members)")
    print(f"  Balance ₹{summary.current_balance:,.2f} | income ₹{summary.total_income:,.2f} | "
          f"expenses ₹{summary.total_expenses:,.2f} | reimbursements owed ₹{summary.pending_reimbursements:,.2f}")
    for event in (freshers, tech_fest, bake_sale):
        result = await ledger_service.get_event_profit_loss(session, club.id, event.id)
        print(f"  {event.name}: {result.result} of ₹{abs(result.net_profit_loss):,.2f}")
    print("\nDemo sign-ins at /dev/login:")
    print(f"  Volunteer  {aditi.full_name}\n  Event lead {lead_events.full_name} (Freshers' Night, Bake Sale)")
    print(f"  Treasurer  {treasurer.full_name}\n  President  {president.full_name}\n  Member     {members[0].full_name}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="drop and recreate all tables first")
    parser.add_argument("--force", action="store_true", help="allow --reset on a database that is not local")
    args = parser.parse_args()

    url = get_settings().database_url
    if args.reset:
        if not args.force and not any(host in url for host in LOCAL_HOSTS):
            raise SystemExit("Refusing to --reset a database that is not on localhost. Pass --force if you mean it.")
        await run_migrations(asyncpg_dsn(url), reset=True)
    else:
        await run_migrations(asyncpg_dsn(url))

    async with SessionLocal() as session:
        if await session.scalar(select(func.count()).select_from(User)):
            raise SystemExit("This database already has users. Re-run with --reset to start from scratch.")
        await seed(session)


if __name__ == "__main__":
    asyncio.run(main())
