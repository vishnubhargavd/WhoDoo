"""Public event registration: venues, the participant limit, payment, QR tickets, email and retention."""

import asyncio
import email
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select, text

from app.api.v1 import public_events
from app.core.config import get_settings
from app.core.security import create_access_token
from app.models.ledger import LedgerEntry
from app.models.registration import EventRegistration
from app.services import gatesync_service, payment_service, registration_service

API = "/api/v1"
QR = 'class="segno"'   # a QR code on a page, as distinct from the bell icon in the top bar
NOW = datetime.now(timezone.utc)
R = EventRegistration


@pytest.fixture(autouse=True)
def _fresh():
    provider = payment_service.get_provider()
    provider.refunds.clear()
    provider.refund_results.clear()
    for path in Path(get_settings().mail_outbox_dir).glob("*.eml"):
        path.unlink()


def code(response) -> str:
    return response.json()["error"]["code"]


def person(n: int = 1, **changes) -> dict:
    return {"full_name": f"Student {n}", "phone": f"98765{n:05d}", "email": f"student{n}@college.example",
            "student_id": f"22CS{n:04d}", **changes}


async def venue_id(client, fits: int) -> str:
    return (await client.get(f"{API}/public/venues?fits={fits}")).json()[0]["id"]


async def make_event(client, auth, world, *, limit=3, mode="free", price=None, publish=True, name="Hack Night") -> str:
    body = {"name": name, "starts_at": (NOW + timedelta(days=3)).isoformat(),
            "ends_at": (NOW + timedelta(days=3, hours=3)).isoformat(), "registration_mode": mode,
            "max_participants": limit, "venue_id": await venue_id(client, limit)}
    if price:
        body["price_paise"] = price
    created = await client.post(f"{API}/events", json=body, headers=auth(world.lead))
    assert created.status_code == 201, created.text
    if publish:
        published = await client.post(f"{API}/events/{created.json()['id']}/status", json={"status": "published"},
                                      headers=auth(world.lead))
        assert published.status_code == 200, published.text
    return created.json()["id"]


async def register(client, event_id, n=1, **changes):
    return await client.post(f"{API}/public/events/{event_id}/register", json=person(n, **changes))


def token_of(registration: dict) -> str:
    return registration["ticket_url"].rsplit("/", 1)[1]


async def scan(client, auth, user, event_id, credential):
    return (await client.post(f"{API}/check-in/scan", json={"event_id": event_id, "credential": credential},
                              headers=auth(user))).json()


async def credential_for(db, registration: dict) -> str:
    registration_id = await db.scalar(select(R.id).where(R.participant_code == registration["participant_code"]))
    return gatesync_service.credential_for(registration_id)


# --- Venues and setting an event up -------------------------------------------------------


async def test_ten_venues_and_only_those_that_fit_are_offered(client):
    venues = (await client.get(f"{API}/public/venues")).json()
    assert len(venues) == 10 and [v["capacity"] for v in venues] == sorted(v["capacity"] for v in venues)
    fits = (await client.get(f"{API}/public/venues?fits=400")).json()
    assert fits and all(v["capacity"] >= 400 for v in fits) and len(fits) < 10
    assert (await client.get(f"{API}/public/venues?fits=5000")).json() == []


async def test_an_event_needs_a_limit_a_price_if_paid_and_a_venue_that_fits(client, world, auth):
    base = {"name": "Quiz", "starts_at": (NOW + timedelta(days=3)).isoformat(),
            "ends_at": (NOW + timedelta(days=3, hours=2)).isoformat()}
    no_limit = await client.post(f"{API}/events", json={**base, "registration_mode": "free"}, headers=auth(world.lead))
    assert (no_limit.status_code, code(no_limit)) == (400, "participants_required")
    no_price = await client.post(f"{API}/events", headers=auth(world.lead),
                                 json={**base, "registration_mode": "paid", "max_participants": 50})
    assert (no_price.status_code, code(no_price)) == (400, "price_required")

    small = await venue_id(client, 1)                      # the smallest room holds 40
    draft = await client.post(f"{API}/events", headers=auth(world.lead),
                              json={**base, "registration_mode": "free", "max_participants": 500, "venue_id": small})
    assert draft.status_code == 201 and draft.json()["venue"] == "Conference Room 204"      # a draft may be incomplete
    event_id = draft.json()["id"]
    refused = await client.post(f"{API}/events/{event_id}/status", json={"status": "published"}, headers=auth(world.lead))
    assert (refused.status_code, code(refused)) == (400, "registration_incomplete")
    assert "holds 500" in refused.json()["error"]["message"]
    fixed = await client.patch(f"{API}/events/{event_id}", json={"venue_id": await venue_id(client, 500)}, headers=auth(world.lead))
    assert fixed.status_code == 200 and fixed.json()["venue"] == "Indoor Sports Complex"
    assert (await client.post(f"{API}/events/{event_id}/status", json={"status": "published"}, headers=auth(world.lead))).status_code == 200
    # Once published it cannot be moved somewhere too small.
    shrink = await client.patch(f"{API}/events/{event_id}", json={"venue_id": small}, headers=auth(world.lead))
    assert (shrink.status_code, code(shrink)) == (400, "registration_incomplete")


# --- Public pages -------------------------------------------------------------------------


async def test_anyone_can_see_published_events_and_drafts_stay_private(client, world, auth):
    open_id = await make_event(client, auth, world)
    await make_event(client, auth, world, publish=False, name="Secret Draft")
    listed = (await client.get(f"{API}/public/events")).json()                 # no sign-in at all
    assert [(e["name"], e["club_name"], e["registration_state"], e["places_left"]) for e in listed] == [
        ("Hack Night", "Test Club", "open", 3)]
    page = await client.get("/events")
    assert page.status_code == 200 and "Hack Night" in page.text and "Secret Draft" not in page.text
    assert "Register" in page.text and "3 of 3 places left" in page.text
    detail = await client.get(f"/e/{open_id}")
    assert detail.status_code == 200 and 'name="student_id"' in detail.text and "Seminar" not in detail.text
    # Signed in, the same address is the club's own management page.
    client.cookies.set(get_settings().session_cookie, create_access_token(world.lead.id))
    managed = await client.get("/events")
    assert "Everything your club is planning" in managed.text and "Secret Draft" in managed.text
    assert "Upcoming events" in (await client.get("/explore")).text


# --- Registering --------------------------------------------------------------------------


async def test_free_registration_gives_a_ticket_and_one_place_per_student(client, world, auth, db):
    event_id = await make_event(client, auth, world)
    made = await register(client, event_id, 1)
    assert made.status_code == 201, made.text
    mine = made.json()
    assert (mine["status"], mine["participant_no"], mine["amount_paise"]) == ("registered", 1, 0)
    assert mine["participant_code"].startswith("WD-") and len(mine["participant_code"]) == 9

    same_email = await register(client, event_id, 2, email="STUDENT1@college.example")
    same_id = await register(client, event_id, 3, student_id="22cs0001")
    assert (code(same_email), code(same_id)) == ("already_registered", "already_registered")
    assert mine["ticket_url"] not in same_email.text                    # nobody is handed another person's ticket
    assert await db.scalar(select(func.count()).select_from(R)) == 1

    for bad in ({"phone": "12345"}, {"email": "not-an-email"}, {"student_id": "!!"}, {"full_name": "A"}):
        assert (await register(client, event_id, 9, **bad)).status_code == 422
    bot = await register(client, event_id, 9, website="http://spam.example")
    assert (bot.status_code, code(bot)) == (400, "registration_refused")
    assert (await client.post(f"{API}/public/events/{uuid.uuid4()}/register", json=person(9))).status_code == 404

    ticket = await client.get("/ticket/" + token_of(mine))
    assert ticket.status_code == 200 and ticket.text.count(QR) == 2 and "ticket-sheet print-only" in ticket.text and mine["participant_code"] in ticket.text
    assert (await client.get("/ticket/" + "A" * 43)).status_code == 404


async def test_the_limit_holds_when_everyone_registers_at_once(client, world, auth, db):
    event_id = await make_event(client, auth, world, limit=5)
    answers = await asyncio.gather(*[register(client, event_id, n) for n in range(1, 21)])
    assert sorted(a.status_code for a in answers) == [201] * 5 + [409] * 15
    refused = next(a for a in answers if a.status_code == 409)
    assert code(refused) == "sold_out" and "All 5 places" in refused.json()["error"]["message"]
    assert sorted(a.json()["participant_no"] for a in answers if a.status_code == 201) == [1, 2, 3, 4, 5]
    assert await db.scalar(select(func.count()).select_from(R)) == 5
    listed = (await client.get(f"{API}/public/events/{event_id}")).json()
    assert (listed["registration_state"], listed["places_left"]) == ("full", 0)
    assert "Fully booked" in (await client.get(f"/e/{event_id}")).text


async def test_every_code_is_different_and_only_digests_are_stored(client, world, auth, db):
    event_id = await make_event(client, auth, world, limit=30)
    # Thirty students, some with the same name: the code comes from the registration, not the person.
    made = [(await register(client, event_id, n, full_name="Same Name")).json() for n in range(1, 31)]
    credentials = [await credential_for(db, m) for m in made]
    assert len(set(credentials)) == 30 and len({m["participant_code"] for m in made}) == 30
    assert len({m["ticket_url"] for m in made}) == 30
    stored = list(await db.scalars(select(R.credential_digest)))
    assert sorted(stored) == sorted(gatesync_service.digest_of(c) for c in credentials)
    everything = (await db.execute(text("SELECT string_agg(r::text, ' ') FROM event_registrations r"))).scalar()
    assert not any(c.split(".")[-1] in everything for c in credentials)         # the code itself is nowhere in the table
    # The database itself refuses two registrations with one code.
    with pytest.raises(Exception, match="credential_digest"):
        await db.execute(text("UPDATE event_registrations SET credential_digest = :d"), {"d": stored[0]})
    await db.rollback()


# --- The door -----------------------------------------------------------------------------


async def test_a_code_admits_once_whoever_shows_it(client, world, auth, db):
    event_id = await make_event(client, auth, world)
    other_event = await make_event(client, auth, world, name="Other Night")
    mine = (await register(client, event_id, 1)).json()
    credential = await credential_for(db, mine)

    assert (await client.post(f"{API}/check-in/scan", json={"event_id": event_id, "credential": credential})).status_code == 401
    assert (await scan(client, auth, world.member, event_id, credential))["error"]["code"] == "forbidden"
    forged = gatesync_service.credential_for(uuid.uuid4())                       # right shape, never issued
    assert (await scan(client, auth, world.volunteer, event_id, forged))["status"] == "unrecognized"
    assert (await scan(client, auth, world.volunteer, event_id, credential[:-2] + "AA"))["status"] in ("unrecognized", "invalid_credential")
    assert (await scan(client, auth, world.volunteer, other_event, credential))["status"] == "wrong_event"

    first = await scan(client, auth, world.volunteer, event_id, credential)
    assert (first["status"], first["holderName"], first["studentId"], first["participantCode"]) == (
        "redeemed", "Student 1", "22CS0001", mine["participant_code"])
    # A screenshot of the same code, shown by a friend at another scanner.
    second = await scan(client, auth, world.lead, event_id, credential)
    assert second["status"] == "already_checked_in" and second["checkedInAt"] == first["checkedInAt"]
    assert await db.scalar(select(func.count()).select_from(R).where(R.status == "checked_in")) == 1
    used = await client.get("/ticket/" + token_of(mine))
    assert "Checked in" in used.text and QR not in used.text                 # the used ticket shows no code


async def test_many_scanners_reading_one_code_admit_it_once(client, world, auth, db):
    event_id = await make_event(client, auth, world)
    credential = await credential_for(db, (await register(client, event_id, 1)).json())
    answers = await asyncio.gather(*[
        scan(client, auth, user, event_id, credential)
        for user in (world.volunteer, world.lead, world.president, world.treasurer) * 3
    ])
    assert sorted(a["status"] for a in answers) == ["already_checked_in"] * 11 + ["redeemed"]


async def test_door_list_manual_check_in_and_removed_participants(client, world, auth, db):
    event_id = await make_event(client, auth, world)
    kept, dropped = [(await register(client, event_id, n)).json() for n in (1, 2)]
    ids = dict((await db.execute(select(R.participant_code, R.id))).all())
    listing = (await client.get(f"{API}/events/{event_id}/registrations", headers=auth(world.lead))).json()
    assert (listing["places_taken"], listing["max_participants"]) == (2, 3)
    assert (await client.get(f"{API}/events/{event_id}/registrations", headers=auth(world.volunteer))).status_code == 403

    cancel = f"{API}/events/registrations/{ids[dropped['participant_code']]}/cancel"
    assert (await client.post(cancel, headers=auth(world.volunteer))).status_code == 403
    assert (await client.post(cancel, headers=auth(world.lead))).json() == {"status": "cancelled", "refund": None}
    assert (await scan(client, auth, world.volunteer, event_id, await credential_for(db, dropped)))["status"] == "revoked"
    assert (await client.get(f"{API}/public/events/{event_id}")).json()["places_left"] == 2      # the place is free again
    assert (await register(client, event_id, 2)).status_code == 201                              # and they may come back

    door = (await client.get(f"{API}/check-in/events/{event_id}", headers=auth(world.volunteer))).json()
    assert (door["total"], door["checked_in"]) == (2, 0)
    manual = {"event_id": event_id, "registration_id": str(ids[kept["participant_code"]])}
    assert (await client.post(f"{API}/check-in/manual", json=manual, headers=auth(world.volunteer))).json()["status"] == "redeemed"
    assert (await scan(client, auth, world.volunteer, event_id, await credential_for(db, kept)))["status"] == "already_checked_in"
    client.cookies.set(get_settings().session_cookie, create_access_token(world.volunteer.id))
    page = await client.get(f"/check-in/{event_id}")
    assert page.status_code == 200 and "Student 1" in page.text and '1</strong> of <span id="door-total">2</span>' in page.text
    assert "/static/door.js" in page.text and 'id="guest-search"' in page.text and "data-admit-registration" in page.text
    # Each row carries what the in-page confirmation box shows: the name, and the IDs to check it against.
    assert 'data-name="Student 2"' in page.text and "22CS0002" in page.text.split('data-name="Student 2"')[1][:200]
    script = (await client.get("/static/door.js")).text
    assert "confirm-box" in script and "Yes, check in" in script and "window.confirm" not in script
    assert "Hack Night" in (await client.get("/check-in")).text
    # After the event is cancelled nothing admits.
    client.cookies.clear()
    await client.post(f"{API}/events/{event_id}/status", json={"status": "cancelled"}, headers=auth(world.lead))
    assert (await client.get(f"{API}/public/events")).json() == []


# --- Paid events --------------------------------------------------------------------------


async def pay(client, registration: dict, outcome="success") -> dict:
    token = token_of(registration)
    checkout = await client.post(f"{API}/public/registrations/{token}/checkout")
    assert checkout.status_code == 200, checkout.text
    paid = await client.post(f"{API}/public/registrations/{token}/mock", json={"outcome": outcome})
    assert paid.status_code == 200, paid.text
    return paid.json()


async def test_paid_event_holds_the_place_then_confirms_with_one_ledger_entry(client, world, auth, db):
    event_id = await make_event(client, auth, world, mode="paid", price=15000, limit=2)
    held = (await register(client, event_id, 1)).json()
    assert (held["status"], held["amount_paise"]) == ("pending_payment", 15000)
    assert (await client.get(f"{API}/public/events/{event_id}")).json()["places_left"] == 1     # the hold counts
    waiting = await client.get("/ticket/" + token_of(held))
    assert "Pay ₹150.00" in waiting.text and QR not in waiting.text                         # no code before payment
    unpaid = await credential_for(db, held)
    assert (await scan(client, auth, world.volunteer, event_id, unpaid))["status"] == "revoked"

    declined = await pay(client, held, "failure")
    assert declined["outcome"] == "not_paid" and declined["registration"]["status"] == "pending_payment"
    paid = await pay(client, held)
    assert (paid["outcome"], paid["registration"]["status"]) == ("registered", "registered")
    entries = list(await db.scalars(select(LedgerEntry).where(LedgerEntry.source_module == "event_ticket")))
    assert [(e.entry_type, str(e.amount), e.event_id) for e in entries] == [("income", "150.00", uuid.UUID(event_id))]
    # Reported again by the provider's webhook: nothing changes.
    session_id = await db.scalar(select(R.checkout_session_id))
    again = await client.post(f"{API}/public/registrations/{token_of(held)}/verify", json={"session_id": session_id})
    assert again.json()["outcome"] == "already_recorded"
    assert await db.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.source_module == "event_ticket")) == 1
    assert (await client.post(f"{API}/public/registrations/{token_of(held)}/checkout")).status_code == 409
    assert (await scan(client, auth, world.volunteer, event_id, unpaid))["status"] == "redeemed"
    # The price is the event's: a request cannot carry one.
    assert (await register(client, event_id, 2, amount_paise=1)).status_code == 422


async def test_an_expired_hold_frees_the_place_and_a_late_payment_is_refunded(client, world, auth, db):
    event_id = await make_event(client, auth, world, mode="paid", price=15000, limit=1)
    slow = (await register(client, event_id, 1)).json()
    checkout = (await client.post(f"{API}/public/registrations/{token_of(slow)}/checkout")).json()
    assert code(await register(client, event_id, 2)) == "sold_out"
    await db.execute(text("UPDATE event_registrations SET hold_expires_at = now() - interval '1 minute'"))
    await db.commit()
    quick = (await register(client, event_id, 2)).json()                 # the place went to someone else
    assert (await pay(client, quick))["outcome"] == "registered"

    payment_service.get_provider().simulate_payment(checkout["session_id"])   # the slow one pays after all
    late = await client.post(f"{API}/public/registrations/{token_of(slow)}/verify", json={"session_id": checkout["session_id"]})
    assert late.json()["outcome"] == "refund_required" and late.json()["registration"]["status"] == "expired"
    assert [paise for _, paise in payment_service.get_provider().refunds.values()] == [15000]
    kinds = sorted(e.entry_type for e in await db.scalars(select(LedgerEntry).where(LedgerEntry.source_module == "event_ticket")))
    assert kinds == ["income", "income", "refund"]
    assert "refunded" in (await client.get("/ticket/" + token_of(slow))).text
    assert await db.scalar(select(func.count()).select_from(R).where(R.status == "registered")) == 1


async def test_removing_a_paid_participant_refunds_them(client, world, auth, db):
    event_id = await make_event(client, auth, world, mode="paid", price=20000)
    mine = (await register(client, event_id, 1)).json()
    await pay(client, mine)
    registration_id = await db.scalar(select(R.id))
    removed = await client.post(f"{API}/events/registrations/{registration_id}/cancel", headers=auth(world.president))
    assert removed.json() == {"status": "cancelled", "refund": "succeeded"}
    assert await db.scalar(select(R.refund_status)) == "succeeded"
    # With registrations in, free/paid and the price are fixed, and the limit cannot drop below them.
    await register(client, event_id, 2)
    for change in ({"registration_mode": "free"}, {"price_paise": 100}):
        blocked = await client.patch(f"{API}/events/{event_id}", json=change, headers=auth(world.lead))
        assert (blocked.status_code, code(blocked)) == (409, "registrations_exist")


async def test_webhook_needs_a_signature(client, world, auth, db):
    event_id = await make_event(client, auth, world, mode="paid", price=15000)
    held = (await register(client, event_id, 1)).json()
    checkout = (await client.post(f"{API}/public/registrations/{token_of(held)}/checkout")).json()
    provider = payment_service.get_provider()
    provider.simulate_payment(checkout["session_id"])
    body = ('{"id": "evt_r1", "type": "checkout.session.completed", "data": {"object": {"id": "%s", '
            '"metadata": {"event_registration_id": "x"}}}}' % checkout["session_id"]).encode()
    forged = await client.post(f"{API}/public/registrations/webhook", content=body, headers={"Stripe-Signature": "t=1,v1=bad"})
    assert (forged.status_code, code(forged)) == (400, "invalid_signature")
    for expected in ("registered", "already_recorded"):
        sent = await client.post(f"{API}/public/registrations/webhook", content=body,
                                 headers={"Stripe-Signature": provider.sign_webhook(body)})
        assert sent.json() == {"status": expected}


# --- Email, rate limit, retention ---------------------------------------------------------


async def test_the_ticket_email_carries_the_details_and_the_qr_code(client, world, auth, db):
    event_id = await make_event(client, auth, world)
    mine = (await register(client, event_id, 1)).json()        # the route queues the email after responding
    outbox = sorted(Path(get_settings().mail_outbox_dir).glob("*.eml"))
    assert len(outbox) == 1
    message = email.message_from_bytes(outbox[0].read_bytes())
    assert message["To"] == "student1@college.example" and mine["participant_code"] in message["Subject"]
    parts = {part.get_content_type(): part for part in message.walk()}
    body = parts["text/plain"].get_payload(decode=True).decode()
    assert "registered successfully" in body and "Hack Night" in body and mine["ticket_url"] in body
    assert parts["image/png"].get_payload(decode=True).startswith(b"\x89PNG")
    assert "cid:" in parts["text/html"].get_payload(decode=True).decode()
    # A held (unpaid) place gets no ticket email.
    paid_event = await make_event(client, auth, world, mode="paid", price=5000, name="Paid Night")
    held = (await register(client, paid_event, 2)).json()
    assert len(list(Path(get_settings().mail_outbox_dir).glob("*.eml"))) == 1
    refused = await client.post(f"{API}/public/registrations/{token_of(held)}/resend")
    assert (refused.status_code, code(refused)) == (409, "no_ticket")
    await pay(client, held)
    assert len(list(Path(get_settings().mail_outbox_dir).glob("*.eml"))) == 2


async def test_a_failing_mail_server_does_not_fail_the_registration(client, world, auth, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "smtp_host", "127.0.0.1")
    monkeypatch.setattr(get_settings(), "smtp_port", 9)          # nothing listens here
    event_id = await make_event(client, auth, world)
    assert (await register(client, event_id, 1)).status_code == 201
    assert len(list(Path(get_settings().mail_outbox_dir).glob("*.eml"))) == 1      # kept for later instead
    assert await db.scalar(select(R.email_sent_at)) is None


async def test_one_address_cannot_flood_an_event(client, world, auth, monkeypatch):
    monkeypatch.setattr(get_settings(), "registration_rate_limit", 3)
    public_events._recent.clear()
    event_id = await make_event(client, auth, world, limit=30)
    codes = [(await register(client, event_id, n)).status_code for n in range(1, 6)]
    assert codes == [201, 201, 201, 429, 429]
    public_events._recent.clear()


async def test_participants_are_deleted_after_the_retention_period(client, world, auth, db):
    event_id = await make_event(client, auth, world, mode="paid", price=5000)
    await pay(client, (await register(client, event_id, 1)).json())
    await register(client, event_id, 2)
    ends = NOW + timedelta(days=3, hours=3)
    assert await registration_service.purge_expired(db, ends + timedelta(days=59)) == 0
    assert await registration_service.purge_expired(db, ends + timedelta(days=61)) == 2
    await db.commit()
    assert await db.scalar(select(func.count()).select_from(R)) == 0
    # The club's accounts are untouched.
    assert await db.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.source_module == "event_ticket")) == 1


# --- Club users: a participant is not crew, and crew are not participants -------------------


async def register_as(client, auth, user, event_id, n):
    return await client.post(f"{API}/public/events/{event_id}/register", json=person(n), headers=auth(user))


async def test_a_participant_is_not_given_tasks_and_crew_cannot_register(client, world, auth, db):
    event_id = await make_event(client, auth, world)
    tasks = f"{API}/events/{event_id}/tasks"
    # Vik has a task, so he is crew: he cannot also take a participant's place.
    made = await client.post(tasks, json={"title": "Set up chairs", "assignee_id": str(world.volunteer.id)}, headers=auth(world.lead))
    assert made.status_code == 201, made.text
    crew = await register_as(client, auth, world.volunteer, event_id, 1)
    assert (crew.status_code, code(crew)) == (409, "has_tasks")
    by_email = await register(client, event_id, 2, email=world.volunteer.email)             # not signed in, same person
    assert code(by_email) == "has_tasks"

    # Mia registers to attend: from then on neither the lead nor the president can give her a task.
    assert (await register_as(client, auth, world.member, event_id, 3)).status_code == 201
    for boss in (world.lead, world.president):
        refused = await client.post(tasks, json={"title": "Sell tickets", "assignee_id": str(world.member.id)}, headers=auth(boss))
        assert (refused.status_code, code(refused)) == (409, "assignee_is_participant")
    moved = await client.patch(f"{API}/tasks/{made.json()['id']}", json={"assignee_id": str(world.member.id)}, headers=auth(world.president))
    assert (moved.status_code, code(moved)) == (409, "assignee_is_participant")
    # Omar registered without signing in, with the email the club knows him by: the same rule.
    assert (await register(client, event_id, 4, email=world.outsider.email)).status_code == 201
    refused = await client.post(tasks, json={"title": "Posters", "assignee_id": str(world.outsider.id)}, headers=auth(world.lead))
    assert code(refused) == "assignee_is_participant"
    # It is about this event only.
    other = await make_event(client, auth, world, name="Other Night")
    assert (await client.post(f"{API}/events/{other}/tasks", json={"title": "Sell tickets", "assignee_id": str(world.member.id)},
                              headers=auth(world.lead))).status_code == 201
    # If the organiser removes her as a participant she can be given tasks again.
    registration_id = await db.scalar(select(R.id).where(R.user_id == world.member.id))
    await client.post(f"{API}/events/registrations/{registration_id}/cancel", headers=auth(world.lead))
    # (She now holds a task at the other event at the same time, which the clash review flags:
    # a different rule, and one the organiser may override with a reason.)
    again = {"title": "Sell tickets", "assignee_id": str(world.member.id)}
    flagged = await client.post(tasks, json=again, headers=auth(world.lead))
    assert (flagged.status_code, code(flagged)) == (409, "assignment_conflicts")
    assert (await client.post(tasks, json={**again, "override_reason": "Covering both evenings"}, headers=auth(world.lead))).status_code == 201


async def test_tasks_are_allocated_to_members_and_volunteers_who_are_not_attending(client, world, auth, db):
    event_id = await make_event(client, auth, world)
    await client.post(f"{API}/events/{event_id}/tasks", json={"title": "Old task", "assignee_id": str(world.lead2.id)}, headers=auth(world.president))
    await register_as(client, auth, world.member, event_id, 1)
    client.cookies.set(get_settings().session_cookie, create_access_token(world.lead.id))
    page = (await client.get(f"/events/{event_id}")).text
    assert "Allocate task to" in page and "<label>Owner" not in page
    form = page[page.index("Allocate task to"):]
    offered = form[:form.index("</select>")]
    assert "Vik Volunteer (Volunteer)" in offered and "Omar Outsider (Member)" in offered
    assert "Mia Member" not in offered                              # attending this event
    for officer in ("Priya President", "Tara Treasurer", "Leo Lead", "Lina Lead"):
        assert officer not in offered                                # officers run events; tasks go to members and volunteers
    assert "Lina Lead</option>" in page                             # but whoever already holds a task is still shown on it


# --- What the ticket page says about the email --------------------------------------------


async def test_the_page_never_claims_an_email_that_was_not_sent(client, world, auth, db, monkeypatch):
    event_id = await make_event(client, auth, world)
    mine = (await register(client, event_id, 1)).json()
    token = token_of(mine)
    # No mail server is set up in the tests: the email is saved, and the page says no email was sent.
    assert await db.scalar(select(R.email_status)) == "saved"
    page = (await client.get(f"/ticket/{token}")).text
    assert "No email was sent" in page and "was emailed to" not in page and "on its way" not in page
    again = await client.post(f"{API}/public/registrations/{token}/resend")
    assert (again.status_code, code(again)) == (409, "email_not_configured")

    monkeypatch.setattr(get_settings(), "smtp_host", "127.0.0.1")
    monkeypatch.setattr(get_settings(), "smtp_port", 9)             # set up, but nothing answers
    failed = await client.post(f"{API}/public/registrations/{token}/resend")
    assert (failed.status_code, code(failed)) == (502, "email_failed")
    assert await db.scalar(select(R.email_status)) == "failed"
    assert "could not be sent" in (await client.get(f"/ticket/{token}")).text

    sent_messages = []
    monkeypatch.setattr("app.services.email_service.deliver", lambda message: sent_messages.append(message["To"]) or "sent")
    ok = await client.post(f"{API}/public/registrations/{token}/resend")
    assert ok.status_code == 200 and ok.json()["status"] == "sent" and sent_messages == ["student1@college.example"]
    assert "A copy was emailed to student1@college.example" in (await client.get(f"/ticket/{token}")).text


# --- The calendar on the front page -------------------------------------------------------


async def test_the_calendar_shows_every_public_event_including_several_on_one_day(client, world, auth, db):
    import json
    import re

    first = await make_event(client, auth, world, name="Hack Night")
    second = await make_event(client, auth, world, mode="paid", price=15000, name="Movie Night")    # same day
    await make_event(client, auth, world, publish=False, name="Secret Draft")
    gone = await make_event(client, auth, world, name="Called Off")
    await client.post(f"{API}/events/{gone}/status", json={"status": "cancelled"}, headers=auth(world.lead))
    # One that has already happened and was completed: still on the calendar, as over.
    await db.execute(text(
        "INSERT INTO events (id, club_id, name, starts_at, ends_at, status, registration_mode, max_participants, price_paise, "
        "lead_id, venue) SELECT gen_random_uuid(), club_id, 'Last Week', now() - interval '7 days', "
        "now() - interval '7 days' + interval '2 hours', 'completed', 'free', 10, 0, lead_id, venue FROM events WHERE id = :id"), {"id": first})
    await db.commit()
    await register(client, first, 1)

    page = (await client.get("/")).text                                         # no sign-in
    data = json.loads(re.search(r'<script type="application/json" class="cal-data">(.*?)</script>', page, re.S).group(1))
    by_name = {e["name"]: e for e in data}
    assert set(by_name) == {"Hack Night", "Movie Night", "Last Week"}           # no draft, no cancelled
    assert by_name["Hack Night"]["starts_at"][:10] == by_name["Movie Night"]["starts_at"][:10]
    assert (by_name["Hack Night"]["registration_state"], by_name["Hack Night"]["places_left"]) == ("open", 2)
    assert (by_name["Movie Night"]["registration_mode"], by_name["Movie Night"]["price_paise"]) == ("paid", 15000)
    assert by_name["Last Week"]["registration_state"] == "closed"
    assert by_name["Movie Night"]["id"] == second and by_name["Hack Night"]["club_name"] == "Test Club"
    # Nothing private rides along in the page.
    assert "student1@college.example" not in page and "22CS0001" not in page
    # The same calendar heads the public events list.
    assert 'class="cal-data"' in (await client.get("/events")).text


async def test_modules_recorded_as_pending_are_not_applied_twice(db):
    from app.core.config import get_settings as settings
    from app.db.database import asyncpg_dsn
    from app.db.migrate import run_migrations

    # A database that took the optional modules while they lived in migrations/pending/.
    await db.execute(text("UPDATE schema_migrations SET name = 'pending/' || name WHERE name LIKE '03%' OR name LIKE '02%'"))
    await db.commit()
    try:
        assert await run_migrations(asyncpg_dsn(settings().database_url)) == []
    finally:
        await db.execute(text("UPDATE schema_migrations SET name = replace(name, 'pending/', '')"))
        await db.commit()
