"""MemberCore, GateSync, CrewDoo Connect, CertiDoo and LegacyVault, through the API and pages."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text

from app.core.config import Settings, get_settings
from app.core.security import create_access_token
from app.models.community import TicketRedemption
from app.models.crewpulse import CrewTask
from app.models.fairseat import EventTicket
from app.models.ledger import LedgerEntry
from app.models.membercore import Membership, MembershipCheckout
from app.models.shared import User
from app.services import fairseat_pricing, gatesync_service, membership_service, modules, payment_service

API = "/api/v1"
NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    modules.forget()
    provider = payment_service.get_provider()
    provider.refunds.clear()
    provider.refund_results.clear()
    monkeypatch.setattr(fairseat_pricing, "_membership_source", membership_service.MemberCoreMemberships())


async def is_member(db, user) -> bool:
    """The flag as stored now, not as the test's copy of the user remembers it."""
    return await db.scalar(select(User.is_active_member).where(User.id == user.id))


def code(response) -> str:
    return response.json()["error"]["code"]


async def make_plan(client, auth, world, **changes) -> dict:
    body = {"name": "Annual", "price_paise": 50000, "validity_days": 365, "ticket_discount_bps": 2000, **changes}
    response = await client.post(f"{API}/memberships/plans", json=body, headers=auth(world.treasurer))
    assert response.status_code == 201, response.text
    return response.json()


async def pay_online(client, auth, user) -> dict:
    checkout = (await client.post(f"{API}/memberships/checkout", headers=auth(user))).json()
    paid = await client.post(f"{API}/memberships/checkout/mock", json={"session_id": checkout["session_id"]}, headers=auth(user))
    assert paid.status_code == 200, paid.text
    return paid.json()


# --- MemberCore -------------------------------------------------------------------------


async def test_only_officers_manage_plans_and_members_see_active_ones(client, world, auth):
    refused = await client.post(f"{API}/memberships/plans", headers=auth(world.lead),
                                json={"name": "X", "price_paise": 1, "validity_days": 1})
    assert (refused.status_code, code(refused)) == (403, "forbidden")
    plan = await make_plan(client, auth, world)
    hidden = await make_plan(client, auth, world, name="Old", is_active=False)
    seen = (await client.get(f"{API}/memberships/plans?include_hidden=true", headers=auth(world.member))).json()
    assert [p["id"] for p in seen] == [plan["id"]]            # a member cannot ask for hidden plans
    everything = (await client.get(f"{API}/memberships/plans?include_hidden=true", headers=auth(world.treasurer))).json()
    assert {p["id"] for p in everything} == {plan["id"], hidden["id"]}
    gone = await client.post(f"{API}/memberships/enroll", json={"plan_id": hidden["id"]}, headers=auth(world.member))
    assert gone.status_code == 404


async def test_joining_is_pending_until_paid_then_active_with_one_ledger_entry(client, world, auth, db):
    plan = await make_plan(client, auth, world)
    joined = (await client.post(f"{API}/memberships/enroll", json={"plan_id": plan["id"]}, headers=auth(world.outsider))).json()
    assert (joined["status"], joined["awaiting"], joined["amount_due_paise"], joined["discount_bps"]) == ("pending", "payment", 50000, 0)
    assert await is_member(db, world.outsider) is False
    # Asking again picks the same enrollment up rather than owing twice.
    again = (await client.post(f"{API}/memberships/enroll", json={"plan_id": plan["id"]}, headers=auth(world.outsider))).json()
    assert again["membership_id"] == joined["membership_id"]
    assert await db.scalar(text("SELECT count(*) FROM membership_dues")) == 1

    paid = await pay_online(client, auth, world.outsider)
    assert paid["outcome"] == "activated"
    assert (paid["membership"]["status"], paid["membership"]["awaiting"], paid["membership"]["discount_bps"]) == ("active", None, 2000)
    assert await is_member(db, world.outsider) is True
    entries = list(await db.scalars(select(LedgerEntry).where(LedgerEntry.source_module == "membership")))
    assert [(e.entry_type, str(e.amount), e.payer_id) for e in entries] == [("income", "500.00", world.outsider.id)]

    # The same session reported again changes nothing.
    session_id = await db.scalar(select(MembershipCheckout.checkout_session_id))
    repeat = await client.post(f"{API}/memberships/checkout/verify", json={"session_id": session_id}, headers=auth(world.outsider))
    assert repeat.json()["outcome"] == "already_recorded"
    assert await db.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.source_module == "membership")) == 1
    # Nothing is owed any more, and somebody else cannot use the session.
    nothing = await client.post(f"{API}/memberships/checkout", headers=auth(world.outsider))
    assert (nothing.status_code, code(nothing)) == (409, "nothing_owed")
    other = await client.post(f"{API}/memberships/checkout/verify", json={"session_id": session_id}, headers=auth(world.member))
    assert other.status_code == 404


async def test_a_declined_payment_activates_nothing(client, world, auth, db):
    plan = await make_plan(client, auth, world)
    await client.post(f"{API}/memberships/enroll", json={"plan_id": plan["id"]}, headers=auth(world.outsider))
    checkout = (await client.post(f"{API}/memberships/checkout", headers=auth(world.outsider))).json()
    declined = await client.post(f"{API}/memberships/checkout/mock", headers=auth(world.outsider),
                                 json={"session_id": checkout["session_id"], "outcome": "failure"})
    assert declined.json()["outcome"] == "not_paid" and declined.json()["membership"]["status"] == "pending"
    assert await db.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.source_module == "membership")) == 0
    assert (await pay_online(client, auth, world.outsider))["outcome"] == "activated"      # and can be tried again


async def test_the_database_refuses_an_active_membership_without_proof(client, world, auth, db):
    plan = await make_plan(client, auth, world)
    await client.post(f"{API}/memberships/enroll", json={"plan_id": plan["id"]}, headers=auth(world.outsider))
    with pytest.raises(Exception, match="activation proof"):
        await db.execute(text("UPDATE memberships SET status = 'active'"))
    await db.rollback()


async def test_free_plan_needs_an_officer_and_a_reason(client, world, auth, db):
    plan = await make_plan(client, auth, world, name="Honorary", price_paise=0)
    joined = (await client.post(f"{API}/memberships/enroll", json={"plan_id": plan["id"]}, headers=auth(world.outsider))).json()
    assert (joined["status"], joined["awaiting"]) == ("pending", "approval")
    nothing = await client.post(f"{API}/memberships/checkout", headers=auth(world.outsider))
    assert (nothing.status_code, code(nothing)) == (409, "nothing_to_pay")
    url = f"{API}/memberships/members/{world.outsider.id}/waive"
    assert (await client.post(url, json={"reason": "Alumni guest"}, headers=auth(world.outsider))).status_code == 403
    assert (await client.post(url, json={"reason": "x"}, headers=auth(world.president))).status_code == 422
    approved = await client.post(url, json={"reason": "Alumni guest"}, headers=auth(world.president))
    assert approved.json()["status"] == "active"
    # No payment was invented for it.
    assert await db.scalar(text("SELECT count(*) FROM membership_dues_reconciliations")) == 0
    assert await db.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.source_module == "membership")) == 0
    twice = await client.post(url, json={"reason": "Alumni guest"}, headers=auth(world.president))
    assert (twice.status_code, code(twice)) == (409, "nothing_owed")


async def test_cash_payment_is_recorded_by_an_officer_and_a_stale_online_payment_is_refunded(client, world, auth, db):
    plan = await make_plan(client, auth, world)
    await client.post(f"{API}/memberships/enroll", json={"plan_id": plan["id"]}, headers=auth(world.outsider))
    checkout = (await client.post(f"{API}/memberships/checkout", headers=auth(world.outsider))).json()
    url = f"{API}/memberships/members/{world.outsider.id}/offline-payment"
    assert (await client.post(url, json={"reference": "Receipt 14"}, headers=auth(world.lead))).status_code == 403
    cash = await client.post(url, json={"reference": "Receipt 14"}, headers=auth(world.treasurer))
    assert cash.json()["status"] == "active"
    entry = await db.scalar(select(LedgerEntry).where(LedgerEntry.source_module == "membership"))
    assert (str(entry.amount), entry.created_by, entry.payer_id) == ("500.00", world.treasurer.id, world.outsider.id)

    # The member then also pays the open checkout: the dues are settled already, so it goes back.
    late = await client.post(f"{API}/memberships/checkout/mock", json={"session_id": checkout["session_id"]}, headers=auth(world.outsider))
    assert late.json()["outcome"] == "refund_due"
    assert (await db.scalar(select(MembershipCheckout.status))) == "refunded"
    assert [paise for _, paise in payment_service.get_provider().refunds.values()] == [50000]
    assert await db.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.source_module == "membership")) == 1


async def test_renewing_extends_the_term_and_keeps_the_bought_discount_until_paid(client, world, auth, db):
    plan = await make_plan(client, auth, world)
    await client.post(f"{API}/memberships/enroll", json={"plan_id": plan["id"]}, headers=auth(world.outsider))
    first = (await pay_online(client, auth, world.outsider))["membership"]
    # The plan changes; the term already bought does not.
    await client.put(f"{API}/memberships/plans/{plan['id']}", json={"ticket_discount_bps": 500, "price_paise": 60000},
                     headers=auth(world.treasurer))
    assert (await client.get(f"{API}/memberships/me", headers=auth(world.outsider))).json()["discount_bps"] == 2000

    renewing = (await client.post(f"{API}/memberships/enroll", json={"plan_id": plan["id"]}, headers=auth(world.outsider))).json()
    assert (renewing["status"], renewing["awaiting"], renewing["amount_due_paise"], renewing["discount_bps"]) == ("active", "payment", 60000, 2000)
    renewed = (await pay_online(client, auth, world.outsider))["membership"]
    assert renewed["starts_at"] == first["starts_at"] and renewed["discount_bps"] == 500
    gained = datetime.fromisoformat(renewed["expires_at"]) - datetime.fromisoformat(first["expires_at"])
    assert gained == timedelta(days=365)


async def test_cancelling_and_expiry_end_member_standing(client, world, auth, db):
    plan = await make_plan(client, auth, world, validity_days=1)
    for person in (world.outsider, world.volunteer):
        await client.post(f"{API}/memberships/enroll", json={"plan_id": plan["id"]}, headers=auth(person))
        await pay_online(client, auth, person)
    cancelled = await client.post(f"{API}/memberships/members/{world.outsider.id}/cancel", json={"reason": "Asked to leave"},
                                  headers=auth(world.president))
    assert cancelled.json()["status"] == "cancelled"
    assert await membership_service.expire_lapsed(db, world.club.id, NOW + timedelta(days=2)) == 1
    await db.commit()
    assert await is_member(db, world.outsider) is False
    assert await is_member(db, world.volunteer) is False
    assert await db.scalar(select(Membership.status).where(Membership.user_id == world.volunteer.id)) == "expired"
    listed = (await client.get(f"{API}/memberships/members", headers=auth(world.treasurer))).json()
    assert {m["full_name"]: m["status"] for m in listed}["Vik Volunteer"] == "expired"
    assert (await client.get(f"{API}/memberships/members", headers=auth(world.lead))).status_code == 403


async def test_webhook_needs_a_valid_signature_and_activates_once(client, world, auth, db):
    plan = await make_plan(client, auth, world)
    await client.post(f"{API}/memberships/enroll", json={"plan_id": plan["id"]}, headers=auth(world.outsider))
    checkout = (await client.post(f"{API}/memberships/checkout", headers=auth(world.outsider))).json()
    provider = payment_service.get_provider()
    provider.simulate_payment(checkout["session_id"])
    dues_id = str(await db.scalar(text("SELECT id FROM membership_dues")))
    body = ('{"id": "evt_1", "type": "checkout.session.completed", "data": {"object": {"id": "%s", '
            '"metadata": {"membership_dues_id": "%s"}}}}' % (checkout["session_id"], dues_id)).encode()
    forged = await client.post(f"{API}/memberships/webhook", content=body, headers={"Stripe-Signature": "t=1,v1=bad"})
    assert (forged.status_code, code(forged)) == (400, "invalid_signature")
    for expected in ("activated", "already_recorded"):
        sent = await client.post(f"{API}/memberships/webhook", content=body,
                                 headers={"Stripe-Signature": provider.sign_webhook(body)})
        assert sent.json() == {"status": expected}
    assert (await client.get(f"{API}/memberships/me", headers=auth(world.outsider))).json()["status"] == "active"


# --- Tickets, member pricing and the door ------------------------------------------------


async def ticketed_event(client, auth, world, db, *, price=20000, capacity=10) -> tuple[str, str]:
    """Publish the bake sale with one ticket category on sale now. Returns (event id, category id)."""
    await db.execute(
        text("UPDATE events SET status = 'published', starts_at = :s, ends_at = :e WHERE id = :id"),
        {"s": NOW + timedelta(days=5), "e": NOW + timedelta(days=5, hours=3), "id": world.bake_sale.id},
    )
    await db.commit()
    saved = await client.put(f"{API}/events/{world.bake_sale.id}/tickets", headers=auth(world.president), json={
        "capacity": capacity, "opens_at": (NOW - timedelta(days=1)).isoformat(),
        "closes_at": (NOW + timedelta(days=4)).isoformat(), "sales_enabled": True,
        "categories": [{"name": "General", "price_paise": price, "capacity": capacity}],
    })
    assert saved.status_code == 200, saved.text
    return str(world.bake_sale.id), saved.json()["categories"][0]["id"]


async def book(client, auth, user, event_id, category_id, quantity=1) -> dict:
    request = {"event_id": event_id, "ticket_category_id": category_id, "quantity": quantity}
    quote = await client.post(f"{API}/ticket-bookings/quotes", json=request, headers=auth(user))
    assert quote.status_code == 201, quote.text
    booking = await client.post(f"{API}/ticket-bookings", headers=auth(user), json={
        **request, "idempotency_key": uuid.uuid4().hex, "accepted_quote_id": quote.json()["id"]})
    assert booking.status_code == 201, booking.text
    return {"quote": quote.json(), **booking.json()}


async def test_an_active_membership_lowers_the_ticket_price_and_a_pending_one_does_not(client, world, auth, db):
    event_id, category_id = await ticketed_event(client, auth, world, db)
    plan = await make_plan(client, auth, world)
    request = {"event_id": event_id, "ticket_category_id": category_id, "quantity": 2}
    await client.post(f"{API}/memberships/enroll", json={"plan_id": plan["id"]}, headers=auth(world.outsider))
    pending = (await client.post(f"{API}/ticket-bookings/quotes", json=request, headers=auth(world.outsider))).json()
    assert (pending["unit_price_paise"], pending["discount_applied"]) == (20000, False)
    await pay_online(client, auth, world.outsider)
    member = (await client.post(f"{API}/ticket-bookings/quotes", json=request, headers=auth(world.outsider))).json()
    assert (member["unit_price_paise"], member["total_price_paise"], member["applied_discount_bps"]) == (16000, 32000, 2000)
    # users.is_active_member alone grants nothing: Mia has the flag but no membership record.
    flagged = (await client.post(f"{API}/ticket-bookings/quotes", json=request, headers=auth(world.member))).json()
    assert (flagged["unit_price_paise"], flagged["discount_reason"]) == (20000, "missing_membership")


def test_credentials_are_opaque_stable_and_in_the_shared_format():
    ticket_id = uuid.uuid4()
    credential = gatesync_service.credential_for(ticket_id)
    assert credential == gatesync_service.credential_for(ticket_id) != gatesync_service.credential_for(uuid.uuid4())
    assert credential.startswith("whodoo.ticket.v1.") and len(credential) == 17 + 43
    assert str(ticket_id) not in credential and ticket_id.hex not in credential
    assert len(gatesync_service.digest_of(credential)) == 32
    for bad in ("", "whodoo.ticket.v1.", credential[:-1], credential + "A", credential.replace("v1", "v2"), None, 7,
                credential[:-1] + "B"):   # the last character must carry zero padding bits
        assert gatesync_service.digest_of(bad) is None
    assert "<svg" in gatesync_service.qr_svg(credential)


def test_a_real_deployment_must_set_a_credential_secret():
    remote = "postgresql+asyncpg://user:secret@db.example.supabase.co:5432/postgres"
    weak = dict(database_url=remote, auth_mode="supabase", demo_auth=False, jwt_secret="dev-only-change-me", supabase_url="https://x.supabase.co", fairseat_enabled=True)
    with pytest.raises(RuntimeError, match="TICKET_CREDENTIAL_SECRET"):
        Settings(**weak).check()
    Settings(**weak, ticket_credential_secret="s" * 40).check()
    Settings(**{**weak, "fairseat_enabled": False}).check()


async def test_a_ticket_is_admitted_once_and_every_other_scan_says_why(client, world, auth, db):
    event_id, category_id = await ticketed_event(client, auth, world, db, price=0)
    booking = await book(client, auth, world.member, event_id, category_id, quantity=2)
    assert booking["status"] == "confirmed" and [t["status"] for t in booking["tickets"]] == ["issued", "issued"]
    first, second = [gatesync_service.credential_for(uuid.UUID(t["id"])) for t in booking["tickets"]]

    async def scan(credential, user=world.volunteer, event=event_id):
        return await client.post(f"{API}/check-in/scan", json={"event_id": event, "credential": credential}, headers=auth(user))

    refused = await scan(first, user=world.member)                      # a member cannot work the door
    assert (refused.status_code, code(refused)) == (403, "forbidden")
    assert (await scan("hello")).json() == {"status": "invalid_credential"}
    assert (await scan(gatesync_service.credential_for(uuid.uuid4()))).json() == {"status": "unrecognized"}
    assert (await scan(first, event=str(world.fest.id))).json()["status"] == "wrong_event"

    admitted = (await scan(first)).json()
    assert (admitted["status"], admitted["eventName"], admitted["holderName"], admitted["seatNumber"]) == (
        "redeemed", "Bake Sale", "Mia Member", 1)
    assert datetime.fromisoformat(admitted["checkedInAt"].replace("Z", "+00:00")).tzinfo is not None
    again = (await scan(first, user=world.lead)).json()
    assert again["status"] == "already_checked_in" and again["checkedInAt"] == admitted["checkedInAt"]
    assert await db.scalar(select(func.count()).select_from(TicketRedemption)) == 1

    door = (await client.get(f"{API}/check-in/events/{event_id}", headers=auth(world.volunteer))).json()
    assert (door["total"], door["checked_in"]) == (2, 1)
    # The second seat comes in from the guest list, once.
    manual = {"event_id": event_id, "ticket_id": booking["tickets"][1]["id"]}
    assert (await client.post(f"{API}/check-in/manual", json=manual, headers=auth(world.volunteer))).json()["status"] == "redeemed"
    assert (await scan(second)).json()["status"] == "already_checked_in"
    assert await db.scalar(select(func.count()).select_from(TicketRedemption).where(TicketRedemption.method == "manual")) == 1
    # Only digests are stored, never the code itself.
    stored = list(await db.scalars(select(EventTicket.credential_digest)))
    assert sorted(stored) == sorted(gatesync_service.digest_of(c) for c in (first, second))


async def test_two_scanners_reading_one_code_admit_it_once(client, world, auth, db):
    import asyncio

    event_id, category_id = await ticketed_event(client, auth, world, db, price=0)
    booking = await book(client, auth, world.member, event_id, category_id)
    body = {"event_id": event_id, "credential": gatesync_service.credential_for(uuid.UUID(booking["tickets"][0]["id"]))}
    answers = await asyncio.gather(*[
        client.post(f"{API}/check-in/scan", json=body, headers=auth(user))
        for user in (world.volunteer, world.lead, world.president, world.treasurer)
    ])
    assert sorted(a.json()["status"] for a in answers) == ["already_checked_in"] * 3 + ["redeemed"]
    assert await db.scalar(select(func.count()).select_from(TicketRedemption)) == 1


async def test_cancelled_tickets_and_cancelled_events_do_not_admit(client, world, auth, db):
    event_id, category_id = await ticketed_event(client, auth, world, db, price=0)
    kept = await book(client, auth, world.member, event_id, category_id)
    dropped = await book(client, auth, world.outsider, event_id, category_id)
    await client.post(f"{API}/ticket-bookings/{dropped['booking_id']}/cancel", headers=auth(world.outsider))

    async def scan(booking):
        credential = gatesync_service.credential_for(uuid.UUID(booking["tickets"][0]["id"]))
        return (await client.post(f"{API}/check-in/scan", json={"event_id": event_id, "credential": credential},
                                  headers=auth(world.volunteer))).json()["status"]

    assert await scan(dropped) == "revoked"
    cancelled = await client.post(f"{API}/events/{event_id}/status", json={"status": "cancelled"},
                                  headers=auth(world.president))
    assert cancelled.status_code == 200, cancelled.text
    assert await scan(kept) in ("revoked", "expired")
    assert await db.scalar(select(func.count()).select_from(TicketRedemption)) == 0


async def test_ticket_pages_show_the_code_only_to_its_holder(client, world, auth, db):
    event_id, category_id = await ticketed_event(client, auth, world, db, price=0)
    booking = await book(client, auth, world.member, event_id, category_id)
    client.cookies.set(get_settings().session_cookie, create_access_token(world.member.id))
    mine = await client.get("/tickets/mine")
    assert mine.status_code == 200 and mine.text.count("<svg") == 1 and "Bake Sale" in mine.text
    assert gatesync_service.credential_for(uuid.UUID(booking["tickets"][0]["id"])) not in mine.text   # in the QR only
    assert "Bake Sale" in (await client.get("/tickets")).text
    assert (await client.get("/check-in")).status_code == 403
    client.cookies.set(get_settings().session_cookie, create_access_token(world.outsider.id))
    assert "<svg" not in (await client.get("/tickets/mine")).text
    client.cookies.set(get_settings().session_cookie, create_access_token(world.volunteer.id))
    door = await client.get(f"/check-in/{event_id}")
    assert door.status_code == 200 and "Mia Member" in door.text and "0</strong> of 1" in door.text
    assert "Bake Sale" in (await client.get("/check-in")).text


# --- CrewDoo Connect --------------------------------------------------------------------


async def test_announcements_reach_only_their_audience(client, world, auth, db):
    async def post(user, **body):
        return await client.post(f"{API}/announcements", headers=auth(user),
                                 json={"title": body.pop("title"), "body": "Details inside.", **body})

    assert (await post(world.member, title="Nope")).status_code == 403
    assert (await post(world.lead, title="Bad", audience="event")).status_code == 422       # an event must be named
    for title, extra in (("All", {}), ("Members", {"audience": "members"}), ("Crew", {"audience": "volunteers"}),
                         ("Officers", {"audience": "officers", "pinned": True}),
                         ("Bake", {"audience": "event", "event_id": str(world.bake_sale.id)})):
        assert (await post(world.president, title=title, **extra)).status_code == 201
    db.add(CrewTask(club_id=world.club.id, event_id=world.bake_sale.id, title="Stall", assignee_id=world.outsider.id,
                    created_by=world.lead.id))
    await db.commit()

    async def titles(user):
        return {a["title"] for a in (await client.get(f"{API}/announcements", headers=auth(user))).json()}

    assert await titles(world.member) == {"All", "Members"}                 # an active member, on no event
    assert await titles(world.outsider) == {"All", "Bake"}                  # not a member, has a task on the event
    assert await titles(world.volunteer) == {"All", "Members", "Crew"}
    assert await titles(world.lead2) == {"All", "Members", "Crew", "Officers", "Bake"}
    feed = (await client.get(f"{API}/announcements", headers=auth(world.lead2))).json()
    assert feed[0]["title"] == "Officers" and feed[0]["read_count"] == 0     # pinned first

    hidden = next(a["id"] for a in feed if a["title"] == "Officers")
    assert (await client.post(f"{API}/announcements/{hidden}/read", headers=auth(world.member))).status_code == 404
    assert (await client.post(f"{API}/announcements/read", headers=auth(world.member))).json() == {"marked": 2}
    assert (await client.post(f"{API}/announcements/read", headers=auth(world.member))).json() == {"marked": 0}
    assert all(a["read"] for a in (await client.get(f"{API}/announcements", headers=auth(world.member))).json())

    everyone = next(a["id"] for a in feed if a["title"] == "All")
    assert (await client.post(f"{API}/announcements/{everyone}/remove", headers=auth(world.lead))).status_code == 403
    assert (await client.post(f"{API}/announcements/{everyone}/remove", headers=auth(world.president))).status_code == 200
    assert await titles(world.member) == {"Members"}


# --- CertiDoo ---------------------------------------------------------------------------


async def test_certificates_follow_attendance_and_can_be_checked_by_anyone(client, world, auth, db):
    event_id, category_id = await ticketed_event(client, auth, world, db, price=0)
    came = await book(client, auth, world.member, event_id, category_id)
    await book(client, auth, world.outsider, event_id, category_id)                     # booked, never arrived
    await client.post(f"{API}/check-in/manual", json={"event_id": event_id, "ticket_id": came["tickets"][0]["id"]},
                      headers=auth(world.volunteer))
    body = {"event_id": event_id, "kind": "participation"}
    assert (await client.post(f"{API}/certificates/for-event", json=body, headers=auth(world.lead2))).status_code == 403
    issued = await client.post(f"{API}/certificates/for-event", json=body, headers=auth(world.lead))   # leads this event
    assert issued.json() == {"issued": 1, "already_had": 0}
    assert (await client.post(f"{API}/certificates/for-event", json=body, headers=auth(world.lead))).json() == {"issued": 0, "already_had": 1}

    mine = (await client.get(f"{API}/certificates", headers=auth(world.member))).json()
    assert [(c["title"], c["recipient_name"]) for c in mine] == [("Participation in Bake Sale", "Mia Member")]
    assert (await client.get(f"{API}/certificates", headers=auth(world.outsider))).json() == []
    certificate = mine[0]
    spaced = certificate["verify_code"][:4].lower() + "-" + certificate["verify_code"][4:]
    public = (await client.get(f"{API}/certificates/verify/{spaced}")).json()               # no sign-in
    assert (public["valid"], public["recipient_name"], public["club_name"]) == (True, "Mia Member", "Test Club")
    assert "email" not in public and "recipient_user_id" not in public
    assert (await client.get(f"{API}/certificates/verify/AAAAAAAAAAAAAAAA")).json() == {"valid": False, "status": "not_found"}
    page = await client.get(f"/verify/{certificate['verify_code']}")
    assert page.status_code == 200 and "Mia Member" in page.text and "Valid" in page.text

    revoke = f"{API}/certificates/{certificate['id']}/revoke"
    assert (await client.post(revoke, json={"reason": "Issued in error"}, headers=auth(world.member))).status_code == 403
    assert (await client.post(revoke, json={"reason": "Issued in error"}, headers=auth(world.lead))).status_code == 200
    assert (await client.get(f"{API}/certificates/verify/{certificate['verify_code']}")).json()["status"] == "revoked"
    # A revoked one no longer blocks issuing again.
    assert (await client.post(f"{API}/certificates/for-event", json=body, headers=auth(world.lead))).json()["issued"] == 1


async def test_volunteering_and_single_certificates(client, world, auth, db):
    db.add(CrewTask(club_id=world.club.id, event_id=world.bake_sale.id, title="Stall", assignee_id=world.volunteer.id,
                    created_by=world.lead.id, status="done", completed_at=NOW))
    db.add(CrewTask(club_id=world.club.id, event_id=world.bake_sale.id, title="Posters", assignee_id=world.member.id,
                    created_by=world.lead.id, status="todo"))
    await db.commit()
    bulk = await client.post(f"{API}/certificates/for-event", headers=auth(world.treasurer),
                             json={"event_id": str(world.bake_sale.id), "kind": "volunteering"})
    assert bulk.json() == {"issued": 1, "already_had": 0}
    one = {"recipient_user_id": str(world.member.id), "title": "Best Newcomer", "kind": "achievement"}
    assert (await client.post(f"{API}/certificates", json=one, headers=auth(world.lead))).status_code == 403
    made = await client.post(f"{API}/certificates", json=one, headers=auth(world.president))
    assert made.status_code == 201 and made.json()["issued_by_name"] == "Priya President"
    client.cookies.set(get_settings().session_cookie, create_access_token(world.member.id))
    printed = await client.get(f"/certificates/{made.json()['id']}")
    assert printed.status_code == 200 and "Best Newcomer" in printed.text and "<svg" in printed.text
    client.cookies.set(get_settings().session_cookie, create_access_token(world.outsider.id))
    assert (await client.get(f"/certificates/{made.json()['id']}")).status_code == 404


# --- LegacyVault ------------------------------------------------------------------------


async def test_handover_notes_are_scoped_by_role_and_keep_their_history(client, world, auth):
    async def write(user, role, title="How it works"):
        return await client.post(f"{API}/handover", json={"role": role, "title": title, "body": "First."}, headers=auth(user))

    assert (await write(world.member, "everyone")).status_code == 403
    assert (await write(world.lead, "treasurer")).status_code == 403            # not their role
    note = (await write(world.treasurer, "treasurer", "Closing the books")).json()
    assert (await write(world.lead, "volunteer", "Stall setup")).status_code == 201
    assert (await write(world.president, "everyone", "Welcome")).status_code == 201

    async def titles(user):
        return {n["title"] for n in (await client.get(f"{API}/handover", headers=auth(user))).json()}

    assert await titles(world.member) == {"Welcome"}
    assert await titles(world.volunteer) == {"Welcome", "Stall setup"}
    assert await titles(world.lead) == {"Welcome", "Stall setup", "Closing the books"}

    url = f"{API}/handover/{note['id']}"
    assert (await client.put(url, json={"title": "x", "body": "y"}, headers=auth(world.lead))).status_code == 403
    assert (await client.put(url, json={"title": "x", "body": "y"}, headers=auth(world.member))).status_code == 404
    saved = await client.put(url, json={"title": "Closing the books", "body": "Second."}, headers=auth(world.president))
    assert (saved.json()["version"], saved.json()["updated_by_name"]) == (2, "Priya President")
    history = (await client.get(f"{url}/versions", headers=auth(world.lead))).json()
    assert [(v["version"], v["body"], v["saved_by_name"]) for v in history] == [(1, "First.", "Tara Treasurer")]

    assert (await client.post(f"{url}/archive", headers=auth(world.treasurer))).json()["archived"] is True
    assert "Closing the books" not in await titles(world.lead)
    archived = (await client.get(f"{API}/handover?include_archived=true", headers=auth(world.lead))).json()
    assert "Closing the books" in {n["title"] for n in archived}


# --- Pages ------------------------------------------------------------------------------


async def test_module_pages_render_for_members_and_officers(client, world, auth):
    await make_plan(client, auth, world)
    for user, expected in ((world.member, 200), (world.treasurer, 200)):
        client.cookies.set(get_settings().session_cookie, create_access_token(user.id))
        for path in ("/memberships", "/tickets", "/tickets/mine", "/announcements", "/certificates", "/handover", "/home"):
            page = await client.get(path)
            assert page.status_code == expected, (path, page.text[:300])
    members = (await client.get("/memberships")).text
    assert "Add a plan" in members and "Mia Member" in members
    client.cookies.set(get_settings().session_cookie, create_access_token(world.member.id))
    assert "Add a plan" not in (await client.get("/memberships")).text
    client.cookies.clear()
    assert (await client.get("/verify")).status_code == 200       # the public check needs no sign-in
