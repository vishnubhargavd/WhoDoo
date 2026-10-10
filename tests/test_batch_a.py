"""Batch A (Member 2): UPI profiles, Razorpay, payouts, profiles, renewal reminders,
registrations with low-ticket warnings, and event budgets. Runs on the disposable test database."""

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.dependencies import CurrentUser
from app.models.expense import ExpenseClaim
from app.models.ledger import LedgerEntry
from app.models.member2 import MembershipRenewalReminder, ReimbursementPayout, UserPaymentProfile
from app.models.membercore import Membership
from app.models.shared import Event, User
from app.services import event_insights, modules, payment_service, payout_service, reminder_service
from app.services.razorpay_provider import RazorpayPaymentProvider, sign

API = "/api/v1"
NOW = datetime.now(timezone.utc)
RECEIPT = (Path(__file__).resolve().parents[1] / "app" / "static" / "samples" / "receipt_bake_sale_850.png").read_bytes()


@pytest.fixture(autouse=True)
def _fresh():
    modules.forget()
    mock = payout_service._mock()
    mock.payouts.clear()
    mock.next_status = "processed"


def code(response) -> str:
    return response.json()["error"]["code"]


def acting(user) -> CurrentUser:
    return CurrentUser(id=user.id, club_id=user.club_id, role=user.role, is_active_member=user.is_active_member,
                       full_name=user.full_name, email=user.email)


@pytest.fixture
def submit(client, auth):
    async def _submit(user, amount="850.00", event=None):
        upload = await client.post(f"{API}/expenses/ocr", headers=auth(user), files={"file": ("r.png", RECEIPT, "image/png")})
        assert upload.status_code == 200, upload.text
        body = {"receipt_id": upload.json()["receipt_id"], "title": "Supplies", "description": "Event supplies", "category": "supplies",
                "amount": amount, "expense_date": "2026-10-03", "vendor_name": "Shop",
                "event_id": str(event.id) if event else None}
        response = await client.post(f"{API}/expenses", headers=auth(user), json=body)
        assert response.status_code == 201, response.text
        return response.json()
    return _submit


# --- Feature 2A: profile editing ---------------------------------------------------------------


async def test_members_edit_only_their_own_permitted_fields(client, db, world, auth):
    me = auth(world.member)
    saved = await client.put(f"{API}/me/profile", headers=me, json={
        "full_name": "Mia M. Member", "display_name": "Mia", "phone": "+91 98765 43210", "department": "CSE",
        "academic_year": 3})
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert (body["full_name"], body["phone"], body["department"], body["academic_year"]) == (
        "Mia M. Member", "+919876543210", "CSE", 3)
    # Role, sign-in email and membership state are not editable: unknown fields are refused.
    for forbidden in ({"role": "president"}, {"email": "x@y.z"}, {"is_active_member": True},
                      {"expires_at": "2030-01-01T00:00:00Z"}, {"user_id": str(world.president.id)}):
        refused = await client.put(f"{API}/me/profile", headers=me, json=forbidden)
        assert refused.status_code == 422, forbidden
    row = await db.scalar(select(User).where(User.id == world.member.id).execution_options(populate_existing=True))
    assert (row.role, row.email) == ("member", world.member.email)
    # Someone else's profile is untouched.
    assert (await client.get(f"{API}/me/profile", headers=auth(world.lead))).json()["phone"] is None
    bad = await client.put(f"{API}/me/profile", headers=me, json={"phone": "12ab"})
    assert bad.status_code == 422
    cleared = await client.put(f"{API}/me/profile", headers=me, json={"phone": None})
    assert cleared.json()["phone"] is None


# --- Feature 1A: UPI profiles ---------------------------------------------------------------


async def test_upi_id_is_validated_encrypted_masked_and_removable(client, db, world, auth):
    me = auth(world.volunteer)
    invalid = await client.put(f"{API}/me/payment-profile", headers=me, json={"upi_id": "not a upi"})
    assert (invalid.status_code, code(invalid)) == (400, "invalid_upi_id")

    saved = await client.put(f"{API}/me/payment-profile", headers=me, json={"upi_id": " Vik.Volunteer@okhdfc "})
    assert saved.status_code == 200, saved.text
    assert saved.json()["upi_masked"] == "vi***********@okhdfc"
    assert (saved.json()["verification_status"], saved.json()["verified_by_mock"]) == ("verified", True)

    stored = await db.scalar(select(UserPaymentProfile).where(UserPaymentProfile.user_id == world.volunteer.id))
    assert "vik.volunteer" not in stored.upi_ciphertext
    audit = await db.scalar(text("SELECT string_agg(details::text, ' ') FROM audit_logs"))
    assert "vik.volunteer" not in (audit or "")
    # Only your own: another person sees nothing, and there is no route that takes a user id.
    assert (await client.get(f"{API}/me/profile", headers=auth(world.member))).json()["payment"] is None
    smuggled = await client.put(f"{API}/me/payment-profile", headers=auth(world.member),
                                json={"upi_id": "x@okaxis", "user_id": str(world.volunteer.id)})
    assert smuggled.status_code == 422

    rejected = await client.put(f"{API}/me/payment-profile", headers=me, json={"upi_id": "failme@okaxis"})
    assert rejected.json()["verification_status"] == "invalid"

    removed = await client.delete(f"{API}/me/payment-profile", headers=me)
    assert removed.status_code == 204
    assert (await client.get(f"{API}/me/profile", headers=me)).json()["payment"] is None


# --- Feature 1C: reimbursement payouts --------------------------------------------------------


async def approved_claim(client, auth, world, submit, amount="850.00"):
    claim = await submit(world.volunteer, amount=amount, event=world.bake_sale)
    first = await client.post(f"{API}/expenses/{claim['id']}/approve", headers=auth(world.lead))
    assert first.status_code == 200, first.text
    if first.json()["status"] != "approved":
        final = await client.post(f"{API}/expenses/{claim['id']}/approve", headers=auth(world.treasurer))
        assert final.json()["status"] == "approved", final.text
    return claim


async def test_payout_needs_verified_upi_and_marks_paid_only_after_provider_success(client, db, world, auth, submit):
    claim = await approved_claim(client, auth, world, submit)
    url = f"{API}/expenses/{claim['id']}/payout"
    no_upi = await client.post(url, headers=auth(world.treasurer))
    assert (no_upi.status_code, code(no_upi)) == (409, "no_upi_id")

    await client.put(f"{API}/me/payment-profile", headers=auth(world.volunteer), json={"upi_id": "vik@okhdfc"})
    for who, expected in ((world.volunteer, 403), (world.lead, 403), (world.member, 403)):
        assert (await client.post(url, headers=auth(who))).status_code == expected

    paid = await client.post(url, headers=auth(world.treasurer))
    assert paid.status_code == 200, paid.text
    assert (paid.json()["status"], paid.json()["is_mock"], paid.json()["amount_paise"]) == ("processed", True, 85000)
    row = await db.scalar(select(ExpenseClaim).where(ExpenseClaim.id == uuid.UUID(claim["id"])).execution_options(populate_existing=True))
    assert row.status == "paid"
    entry = await db.scalar(select(LedgerEntry).where(LedgerEntry.reference_id == row.id))
    assert entry.payment_status == "paid"
    again = await client.post(url, headers=auth(world.treasurer))
    assert (again.status_code, code(again)) == (409, "payout_exists")


async def test_failed_payout_leaves_claim_approved_and_can_be_retried(client, db, world, auth, submit):
    claim = await approved_claim(client, auth, world, submit)
    await client.put(f"{API}/me/payment-profile", headers=auth(world.volunteer), json={"upi_id": "vik@okhdfc"})
    payout_service._mock().next_status = "failed"
    failed = await client.post(f"{API}/expenses/{claim['id']}/payout", headers=auth(world.treasurer))
    assert failed.json()["status"] == "failed"
    status = await db.scalar(select(ExpenseClaim.status).where(ExpenseClaim.id == uuid.UUID(claim["id"])))
    assert status == "approved"

    payout_service._mock().next_status = "processed"
    retried = await client.post(f"{API}/expenses/{claim['id']}/payout", headers=auth(world.treasurer))
    assert (retried.json()["status"], retried.json()["attempt"]) == ("processed", 2)


async def test_pending_payout_blocks_manual_paid_until_provider_confirms(client, db, world, auth, submit):
    claim = await approved_claim(client, auth, world, submit)
    await client.put(f"{API}/me/payment-profile", headers=auth(world.volunteer), json={"upi_id": "vik@okhdfc"})
    payout_service._mock().next_status = "processing"
    pending = (await client.post(f"{API}/expenses/{claim['id']}/payout", headers=auth(world.treasurer))).json()
    assert pending["status"] == "processing"
    manual = await client.post(f"{API}/expenses/{claim['id']}/mark-paid", headers=auth(world.treasurer))
    assert (manual.status_code, code(manual)) == (409, "payout_in_progress")
    removing = await client.delete(f"{API}/me/payment-profile", headers=auth(world.volunteer))
    assert removing.status_code == 409

    still = await client.post(f"{API}/payouts/{pending['id']}/refresh", headers=auth(world.treasurer))
    assert still.json()["status"] == "processing"
    for result in payout_service._mock().payouts.values():
        result.status = "processed"
    settled = await client.post(f"{API}/payouts/{pending['id']}/refresh", headers=auth(world.treasurer))
    assert settled.json()["status"] == "processed"
    assert await db.scalar(select(ExpenseClaim.status).where(ExpenseClaim.id == uuid.UUID(claim["id"]))) == "paid"


async def test_large_claim_payout_keeps_two_approver_rule(client, world, auth, submit):
    claim = await submit(world.volunteer, amount="2500.00", event=world.bake_sale)
    await client.put(f"{API}/me/payment-profile", headers=auth(world.volunteer), json={"upi_id": "vik@okhdfc"})
    await client.post(f"{API}/expenses/{claim['id']}/approve", headers=auth(world.lead))
    early = await client.post(f"{API}/expenses/{claim['id']}/payout", headers=auth(world.treasurer))
    assert (early.status_code, code(early)) == (409, "not_approved")
    await client.post(f"{API}/expenses/{claim['id']}/approve", headers=auth(world.treasurer))
    paid = await client.post(f"{API}/expenses/{claim['id']}/payout", headers=auth(world.president))
    assert paid.json()["status"] == "processed"

    own = await submit(world.treasurer, amount="100.00")
    await client.post(f"{API}/expenses/{own['id']}/approve", headers=auth(world.president))
    await client.put(f"{API}/me/payment-profile", headers=auth(world.treasurer), json={"upi_id": "tara@okicici"})
    self_pay = await client.post(f"{API}/expenses/{own['id']}/payout", headers=auth(world.treasurer))
    assert (self_pay.status_code, code(self_pay)) == (403, "self_payment")


# --- Feature 1B: Razorpay ------------------------------------------------------------------


class FakeRazorpay:
    """Razorpay's Payment Links API, in memory, behind httpx.MockTransport."""

    def __init__(self):
        self.links: dict[str, dict] = {}
        self.refunds: dict[str, list[dict]] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"].startswith("Basic ")
        path = request.url.path.removeprefix("/v1")
        if request.method == "POST" and path == "/payment_links":
            body = json.loads(request.content)
            link = {"id": f"plink_{uuid.uuid4().hex[:14]}", "status": "created", "amount": body["amount"],
                    "amount_paid": 0, "currency": body["currency"], "notes": body["notes"],
                    "short_url": "https://rzp.io/i/test", "payments": None, "callback_url": body["callback_url"],
                    "reference_id": body["reference_id"]}
            self.links[link["id"]] = link
            return httpx.Response(200, json=link)
        if request.method == "GET" and path.startswith("/payment_links/"):
            link = self.links.get(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json=link) if link else httpx.Response(404, json={"error": {}})
        if path.endswith("/refunds") and request.method == "GET":
            return httpx.Response(200, json={"items": self.refunds.get(path.split("/")[2], [])})
        if path.endswith("/refund") and request.method == "POST":
            body = json.loads(request.content)
            refund = {"id": f"rfnd_{uuid.uuid4().hex[:10]}", "status": "processed", "amount": body["amount"],
                      "notes": body["notes"]}
            self.refunds.setdefault(path.split("/")[2], []).append(refund)
            return httpx.Response(200, json=refund)
        return httpx.Response(404, json={"error": {"description": "unknown"}})

    def pay(self, link_id: str) -> str:
        link = self.links[link_id]
        payment_id = f"pay_{uuid.uuid4().hex[:14]}"
        link.update(status="paid", amount_paid=link["amount"],
                    payments=[{"payment_id": payment_id, "status": "captured", "amount": link["amount"]}])
        return payment_id


@pytest.fixture
def razorpay(monkeypatch):
    fake = FakeRazorpay()
    provider = RazorpayPaymentProvider("rzp_test_abc", "secret", "whsec_rzp", base="https://api.razorpay.test/v1",
                                       transport=httpx.MockTransport(fake.handler))
    monkeypatch.setattr(payment_service, "get_provider", lambda: provider)
    monkeypatch.setattr(get_settings(), "razorpay_webhook_secret", "whsec_rzp")
    return fake


def link_paid_event(link: dict, payment_id: str) -> bytes:
    return json.dumps({"event": "payment_link.paid", "payload": {
        "payment_link": {"entity": link}, "payment": {"entity": {"id": payment_id}}}}).encode()


async def test_razorpay_dues_paid_only_after_signed_webhook_and_server_check(client, db, world, auth, razorpay):
    plan = (await client.post(f"{API}/memberships/plans", headers=auth(world.treasurer),
                              json={"name": "Annual", "price_paise": 50000, "validity_days": 365})).json()
    await client.post(f"{API}/memberships/enroll", json={"plan_id": plan["id"]}, headers=auth(world.outsider))
    checkout = (await client.post(f"{API}/memberships/checkout", headers=auth(world.outsider))).json()
    assert (checkout["provider"], checkout["is_mock"]) == ("razorpay", False)
    link = razorpay.links[checkout["session_id"]]
    assert (link["amount"], link["currency"]) == (50000, "INR")
    assert "/payments/razorpay/return?next=" in link["callback_url"]
    hook = f"{API}/payments/razorpay/webhook"

    # A forged "paid" event for a link Razorpay says is unpaid changes nothing.
    forged = link_paid_event(dict(link, status="paid", amount_paid=50000), "pay_forged")
    response = await client.post(hook, content=forged, headers={"X-Razorpay-Signature": sign(forged, "whsec_rzp")})
    assert response.status_code == 200
    assert await db.scalar(select(Membership.status).where(Membership.user_id == world.outsider.id)) == "pending"

    bad = await client.post(hook, content=forged, headers={"X-Razorpay-Signature": "0" * 64})
    assert (bad.status_code, code(bad)) == (400, "invalid_webhook_signature")

    payment_id = razorpay.pay(link["id"])
    body = link_paid_event(link, payment_id)
    headers = {"X-Razorpay-Signature": sign(body, "whsec_rzp"), "X-Razorpay-Event-Id": "evt_1"}
    first = await client.post(hook, content=body, headers=headers)
    assert first.json()["status"] == "processed", first.text
    status = await db.scalar(select(Membership.status).where(Membership.user_id == world.outsider.id)
                             .execution_options(populate_existing=True))
    assert status == "active"
    duplicate = await client.post(hook, content=body, headers=headers)
    assert duplicate.json()["status"] == "duplicate"
    income = await db.scalar(text("SELECT count(*) FROM ledger_entries WHERE source_module = 'membership'"))
    assert income == 1


def test_razorpay_refund_is_idempotent_per_key():
    fake = FakeRazorpay()
    provider = RazorpayPaymentProvider("rzp_test_abc", "s", "w", base="https://api.razorpay.test/v1",
                                       transport=httpx.MockTransport(fake.handler))
    first = provider.refund("pay_1", 100, "booking:1:refund")
    again = provider.refund("pay_1", 100, "booking:1:refund")
    assert (first.status, first.id) == ("succeeded", again.id)
    assert len(fake.refunds["pay_1"]) == 1
    with pytest.raises(ValueError):
        RazorpayPaymentProvider("rzp_live_abc", "s", "w")


async def test_razorpay_return_never_redirects_off_site(client):
    ok = await client.get("/payments/razorpay/return", params={
        "next": "/memberships/return?session_id={CHECKOUT_SESSION_ID}", "razorpay_payment_link_id": "plink_ABC123def"},
        follow_redirects=False)
    assert ok.headers["location"] == "/memberships/return?session_id=plink_ABC123def"
    for target in ("https://evil.example/x", "//evil.example", "/\\evil"):
        away = await client.get("/payments/razorpay/return", params={"next": target}, follow_redirects=False)
        assert away.headers["location"] == "/home", target


# --- Feature 2B: renewal reminders ---------------------------------------------------------


async def active_member(client, auth, world, user, validity=365):
    plan = (await client.post(f"{API}/memberships/plans", headers=auth(world.treasurer),
                              json={"name": f"Plan {uuid.uuid4().hex[:4]}", "price_paise": 50000,
                                    "validity_days": validity})).json()
    await client.post(f"{API}/memberships/enroll", json={"plan_id": plan["id"]}, headers=auth(user))
    checkout = (await client.post(f"{API}/memberships/checkout", headers=auth(user))).json()
    paid = await client.post(f"{API}/memberships/checkout/mock", json={"session_id": checkout["session_id"]}, headers=auth(user))
    assert paid.json()["outcome"] == "activated", paid.text
    return plan


def test_only_the_stage_reached_is_due():
    offsets = (30, 14, 7, 1)
    expiry = NOW + timedelta(days=5)
    assert reminder_service.stage_for(expiry, NOW, offsets) == "d7"
    assert reminder_service.stage_for(NOW + timedelta(days=40), NOW, offsets) is None
    assert reminder_service.stage_for(NOW + timedelta(hours=3), NOW, offsets) == "d1"
    assert reminder_service.stage_for(NOW - timedelta(days=1), NOW, offsets) == "expired"
    assert reminder_service.stage_for(NOW - timedelta(days=400), NOW, offsets) is None


async def test_reminders_are_written_once_per_stage_and_stop_after_renewal(client, db, world, auth):
    await active_member(client, auth, world, world.outsider, validity=30)
    expires = await db.scalar(select(Membership.expires_at).where(Membership.user_id == world.outsider.id))
    me = acting(world.outsider)

    day_20 = expires - timedelta(days=20)
    assert await reminder_service.generate(db, now=day_20) == 1           # d30
    assert await reminder_service.generate(db, now=day_20) == 0           # idempotent
    day_10 = expires - timedelta(days=10)
    shown = await reminder_service.list_for(db, me, now=day_10)          # writes d14 on the way
    assert [(r.stage, r.read) for r in shown] == [("d14", False), ("d30", True)]
    assert "Renew" in shown[0].message and shown[0].plan_name and shown[0].club_name == "Test Club"
    await db.commit()
    # Someone else cannot mark it read.
    other = await client.post(f"{API}/me/notifications/{shown[0].id}/read", headers=auth(world.member))
    assert other.status_code == 404
    await reminder_service.mark_read(db, me, shown[0].id)
    await db.commit()
    assert await reminder_service.list_for(db, me, now=day_10, unread_only=True) == []

    # Renewing moves the expiry: the old term's reminders stop showing, nothing new is due.
    plan_id = await db.scalar(select(Membership.plan_id).where(Membership.user_id == world.outsider.id))
    await client.post(f"{API}/memberships/enroll", json={"plan_id": str(plan_id)}, headers=auth(world.outsider))
    checkout = (await client.post(f"{API}/memberships/checkout", headers=auth(world.outsider))).json()
    await client.post(f"{API}/memberships/checkout/mock", json={"session_id": checkout["session_id"]}, headers=auth(world.outsider))
    db.expire_all()
    assert await reminder_service.list_for(db, me, now=day_10) == []
    assert await db.scalar(text("SELECT count(*) FROM membership_renewal_reminders")) == 2  # history kept


async def test_cancelled_and_pending_memberships_get_no_reminders(client, db, world, auth):
    await active_member(client, auth, world, world.member, validity=10)
    cancelled = await client.post(f"{API}/memberships/members/{world.member.id}/cancel", headers=auth(world.treasurer),
                                  json={"reason": "left the club"})
    assert cancelled.status_code == 200, cancelled.text
    plan = (await client.post(f"{API}/memberships/plans", headers=auth(world.treasurer),
                              json={"name": "Short", "price_paise": 100, "validity_days": 3})).json()
    await client.post(f"{API}/memberships/enroll", json={"plan_id": plan["id"]}, headers=auth(world.outsider))  # pending
    assert await reminder_service.generate(db, now=NOW + timedelta(days=2)) == 0
    shown = await client.get(f"{API}/me/notifications", headers=auth(world.member))
    assert shown.json() == []


# --- Feature 3: registrations, low tickets, sold out, budget -----------------------------------


@pytest.fixture
async def ticketed(client, db, auth, world):
    event = Event(id=uuid.uuid4(), club_id=world.club.id, name="Gala", lead_id=world.lead.id,
                  starts_at=NOW + timedelta(days=7), ends_at=NOW + timedelta(days=7, hours=3), venue="Hall",
                  status="published", created_by=world.president.id)
    db.add(event)
    await db.commit()
    body = {"sales_enabled": True, "capacity": 3, "max_per_booking": 6,
            "opens_at": (NOW - timedelta(days=1)).isoformat(), "closes_at": event.starts_at.isoformat(),
            "categories": [{"name": "Free", "price_paise": 0, "capacity": 3, "is_visible": True}]}
    saved = await client.put(f"{API}/events/{event.id}/tickets", headers=auth(world.president), json=body)
    assert saved.status_code == 200, saved.text
    category = (await client.get(f"{API}/ticket-categories?event_id={event.id}", headers=auth(world.president))).json()[0]
    return event, category


async def book(client, auth, user, category, quantity=1):
    selection = {"event_id": category["event_id"], "ticket_category_id": category["id"], "quantity": quantity}
    quote = await client.post(f"{API}/ticket-bookings/quotes", headers=auth(user), json=selection)
    if quote.status_code != 201:
        return quote
    return await client.post(f"{API}/ticket-bookings", headers=auth(user), json=selection | {
        "idempotency_key": f"t-{uuid.uuid4().hex}", "accepted_quote_id": quote.json()["id"]})


def test_low_ticket_threshold_is_whichever_is_reached_first():
    assert event_insights.low_threshold(1000) == 100   # 10% first
    assert event_insights.low_threshold(20) == 5       # five seats first
    assert event_insights.level(0, 20) == ("sold_out", "Sold out")
    assert event_insights.level(1, 20) == ("low", "Only 1 ticket left")
    assert event_insights.level(6, 20)[0] == "available"


async def test_last_seat_race_has_one_winner_and_counts_agree(client, auth, world, ticketed):
    event, category = ticketed
    first = await book(client, auth, world.member, category, quantity=2)
    assert first.status_code == 201, first.text
    summary = (await client.get(f"{API}/events/{event.id}/registrations", headers=auth(world.lead))).json()
    assert (summary["confirmed_seats"], summary["remaining"], summary["level"]) == (2, 1, "low")

    racers = await asyncio.gather(book(client, auth, world.volunteer, category), book(client, auth, world.outsider, category))
    assert sorted(r.status_code for r in racers) == [201, 409]
    loser = next(r for r in racers if r.status_code == 409)
    assert code(loser) == "sold_out"

    summary = (await client.get(f"{API}/events/{event.id}/registrations", headers=auth(world.lead))).json()
    assert (summary["total_registrations"], summary["confirmed_registrations"]) == (2, 2)
    assert (summary["confirmed_seats"], summary["remaining"], summary["level"], summary["status_label"]) == (3, 0, "sold_out", "SOLD OUT")
    assert summary["categories"][0]["level"] == "sold_out"
    page = await client.get("/tickets", headers=auth(world.member))
    assert "SOLD OUT" in page.text
    # Only organizers see registration counts.
    assert (await client.get(f"{API}/events/{event.id}/registrations", headers=auth(world.member))).status_code == 403


async def test_budget_reads_spending_from_the_ledger_and_only_warns(client, db, auth, world, submit):
    url = f"{API}/events/{world.bake_sale.id}/budget"
    assert (await client.put(url, headers=auth(world.member), json={"budget_paise": 100})).status_code == 404  # a draft
    assert (await client.put(url, headers=auth(world.lead2), json={"budget_paise": 100})).status_code == 404
    assert (await client.put(url, headers=auth(world.lead), json={"budget_paise": -1})).status_code == 422
    set_ = await client.put(url, headers=auth(world.lead), json={"budget_paise": 100000})
    assert set_.json()["remaining_paise"] == 100000

    paid = await submit(world.volunteer, amount="850.00", event=world.bake_sale)
    await client.post(f"{API}/expenses/{paid['id']}/approve", headers=auth(world.lead))
    await client.post(f"{API}/expenses/{paid['id']}/mark-paid", headers=auth(world.treasurer))
    await submit(world.volunteer, amount="300.00", event=world.bake_sale)        # still pending

    budget = (await client.get(url, headers=auth(world.treasurer))).json()
    assert (budget["spent_paise"], budget["committed_paise"], budget["pending_paise"]) == (85000, 0, 30000)
    assert (budget["remaining_paise"], budget["over_budget"]) == (15000, False)
    assert "pending" in budget["warning"].lower() or "approval" in budget["warning"].lower()

    # Approving past the budget is allowed; the budget only reports it.
    pending = await db.scalar(select(ExpenseClaim.id).where(ExpenseClaim.status == "pending_approval"))
    approved = await client.post(f"{API}/expenses/{pending}/approve", headers=auth(world.lead))
    assert approved.status_code == 200
    page = await client.get(f"/events/{world.bake_sale.id}", headers=auth(world.lead))
    assert page.status_code == 200 and "Budget" in page.text
