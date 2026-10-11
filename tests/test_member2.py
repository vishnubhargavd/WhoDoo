"""Features brought over from Member 2's Batch A: profiles, UPI ids, renewal reminders and the event
budget, plus the notification bell and the certificate that were built around them here.

The profile, reminder and budget tests are his, kept as written. His payout, Razorpay and FairSeat
registration-count tests are not here because those features were not brought across.
"""

import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.dependencies import CurrentUser
from app.core.security import create_access_token
from app.models.expense import ExpenseClaim
from app.models.member2 import UserPaymentProfile
from app.models.membercore import Membership
from app.models.shared import User
from app.services import gatesync_service, modules, reminder_service

API = "/api/v1"
NOW = datetime.now(timezone.utc)
RECEIPT = (Path(__file__).resolve().parents[1] / "app" / "static" / "samples" / "receipt_bake_sale_850.png").read_bytes()


@pytest.fixture(autouse=True)
def _fresh():
    modules.forget()

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



# --- Profile editing --------------------------------------------------------------------------


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


# --- UPI ids ----------------------------------------------------------------------------------


async def test_upi_id_is_validated_encrypted_masked_and_removable(client, db, world, auth):
    me = auth(world.volunteer)
    invalid = await client.put(f"{API}/me/payment-profile", headers=me, json={"upi_id": "not a upi"})
    assert (invalid.status_code, code(invalid)) == (400, "invalid_upi_id")

    saved = await client.put(f"{API}/me/payment-profile", headers=me, json={"upi_id": " Vik.Volunteer@okhdfc "})
    assert saved.status_code == 200, saved.text
    assert saved.json()["upi_masked"] == "vi***********@okhdfc"
    # Nothing here confirms the account with a bank, so it is never shown as verified.
    assert (saved.json()["verification_status"], saved.json()["verified_at"]) == ("unverified", None)

    stored = await db.scalar(select(UserPaymentProfile).where(UserPaymentProfile.user_id == world.volunteer.id))
    assert "vik.volunteer" not in stored.upi_ciphertext
    audit = await db.scalar(text("SELECT string_agg(details::text, ' ') FROM audit_logs"))
    assert "vik.volunteer" not in (audit or "")
    # Only your own: another person sees nothing, and there is no route that takes a user id.
    assert (await client.get(f"{API}/me/profile", headers=auth(world.member))).json()["payment"] is None
    smuggled = await client.put(f"{API}/me/payment-profile", headers=auth(world.member),
                                json={"upi_id": "x@okaxis", "user_id": str(world.volunteer.id)})
    assert smuggled.status_code == 422

    client.cookies.set(get_settings().session_cookie, create_access_token(world.volunteer.id))
    page = (await client.get("/profile")).text
    assert "vi***********@okhdfc" in page and "vik.volunteer@okhdfc" not in page and "Pay out" not in page
    client.cookies.clear()

    removed = await client.delete(f"{API}/me/payment-profile", headers=me)
    assert removed.status_code == 204
    assert (await client.get(f"{API}/me/profile", headers=me)).json()["payment"] is None


# --- Renewal reminders ------------------------------------------------------------------------

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


# --- Event budget -----------------------------------------------------------------------------


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

# --- The notification bell --------------------------------------------------------------------


async def test_the_bell_feed_combines_reminders_and_announcements_and_marks_them_read(client, db, world, auth):
    await active_member(client, auth, world, world.outsider, validity=5)            # a reminder is due already
    posted = await client.post(f"{API}/announcements", headers=auth(world.president),
                               json={"title": "Hall changed", "body": "We are in the Mini Auditorium now."})
    assert posted.status_code == 201
    me = auth(world.outsider)
    feed = (await client.get(f"{API}/me/notifications/feed", headers=me)).json()
    assert feed["unread"] == 2
    assert {(i["kind"], i["href"]) for i in feed["items"]} == {("renewal", "/memberships"), ("announcement", "/announcements")}
    assert "Priya President" in next(i for i in feed["items"] if i["kind"] == "announcement")["message"]
    # Each item says how to mark itself read; doing so takes it off the count.
    for item in feed["items"]:
        assert (await client.post(item["read_url"], headers=me)).status_code in (200, 204)
    after = (await client.get(f"{API}/me/notifications/feed", headers=me)).json()
    assert after["unread"] == 0 and len(after["items"]) == 2
    assert (await client.get(f"{API}/me/notifications/feed?unread_only=true", headers=me)).json()["items"] == []
    # Somebody else's feed is their own: the announcement only.
    assert (await client.get(f"{API}/me/notifications/feed", headers=auth(world.volunteer))).json()["unread"] == 1
    assert (await client.get(f"{API}/me/notifications/feed")).status_code == 401


async def test_the_bell_is_in_the_top_bar_and_notifications_are_in_the_menu(client, world):
    client.cookies.set(get_settings().session_cookie, create_access_token(world.member.id))
    home = (await client.get("/home")).text
    assert 'class="bell"' in home and 'id="bell-panel"' in home and 'href="/notifications">See all' in home
    menu = home[home.index('id="site-menu"'):home.index("</nav>", home.index('id="site-menu"'))]
    assert 'href="/profile"' in menu and 'href="/notifications"' in menu      # both under Account in the three-bar menu
    assert 'href="/notifications" class' not in home                          # but not a tab in the section bar
    page = await client.get("/notifications")
    assert page.status_code == 200 and "Notifications" in page.text
    client.cookies.clear()
    assert 'class="bell"' not in (await client.get("/")).text               # nothing to ring for a visitor


# --- The certificate --------------------------------------------------------------------------


async def test_the_certificate_qr_code_leads_to_its_own_public_check(client, world, auth):
    made = (await client.post(f"{API}/certificates", headers=auth(world.president), json={
        "recipient_user_id": str(world.member.id), "title": "Best Newcomer", "kind": "achievement"})).json()
    client.cookies.set(get_settings().session_cookie, create_access_token(world.member.id))
    page = (await client.get(f"/certificates/{made['id']}")).text
    # The QR code on the page is exactly the code for this certificate's verify link, at the
    # address the page was opened on (so a phone on the same network can follow it).
    link = f"http://test/verify/{made['verify_code']}"
    assert gatesync_service.qr_svg(link) in page and page.count('class="segno"') == 1
    assert 'class="cert-qr"' in page and "kind-achievement" in page and "Mia Member" in page
    code_shown = re.search(r"<code>([A-Z2-7 ]+)</code>", page).group(1)
    assert code_shown.replace(" ", "") == made["verify_code"]
    client.cookies.clear()
    checked = await client.get(link.replace("http://test", ""))                  # no sign-in, as a scanner would
    assert checked.status_code == 200 and "Valid" in checked.text and "Best Newcomer" in checked.text
    typed = await client.get(f"/verify?code={code_shown.replace(' ', '-').lower()}")
    assert "Valid" in typed.text


# --- Task review: who is suggested instead ----------------------------------------------------


async def test_review_suggestions_follow_the_allocate_list(client, db, world, auth):
    starts = NOW + timedelta(days=4)
    created = await client.post(f"{API}/events", headers=auth(world.lead), json={
        "name": "Open Mic", "starts_at": starts.isoformat(), "ends_at": (starts + timedelta(hours=2)).isoformat()})
    event_id = created.json()["id"]
    tasks = f"{API}/events/{event_id}/tasks"
    assert (await client.post(tasks, json={"title": "Book the DJ", "assignee_id": str(world.volunteer.id)},
                              headers=auth(world.lead))).status_code == 201
    review = await client.post(f"{tasks}/review", headers=auth(world.lead),
                               json={"title": "Book DJ", "assignee_id": str(world.volunteer.id)})
    body = review.json()
    assert body["ok"] is False and body["issues"][0]["code"] == "near_duplicate"
    # Only members and volunteers are offered instead, never officers.
    assert {s["role"] for s in body["suggestions"]} <= {"member", "volunteer"} and body["suggestions"]


# --- The three-bar menu and the crew clashes page ---------------------------------------------


def _menu(html: str) -> str:
    start = html.index('id="site-menu"')
    return html[start:html.index("</nav>", start)]


async def test_the_menu_lists_every_module_and_keeps_the_shop_apart_from_running_it(client, world):
    client.cookies.set(get_settings().session_cookie, create_access_token(world.treasurer.id))
    menu = _menu((await client.get("/home")).text)
    for href in ("/memberships", "/announcements", "/certificates", "/handover", "/store", "/orders", "/tickets",
                 "/check-in", "/profile", "/notifications", "/admin/merch", "/admin/merch/orders"):
        assert f'href="{href}"' in menu, href
    # The shop and running the shop are separate groups, the shop first.
    assert menu.index("<h3>Store</h3>") < menu.index("<h3>Merchandise</h3>") < menu.index('href="/admin/merch"')
    assert menu.index('href="/store"') < menu.index("<h3>Merchandise</h3>")
    assert 'href="/crew/review"' not in menu                               # the treasurer does not hand out tasks

    client.cookies.set(get_settings().session_cookie, create_access_token(world.member.id))
    menu = _menu((await client.get("/home")).text)
    assert "<h3>Merchandise</h3>" not in menu and 'href="/admin/merch"' not in menu and 'href="/check-in"' not in menu
    assert 'href="/store"' in menu and 'href="/memberships"' in menu and 'href="/notifications"' in menu
    client.cookies.set(get_settings().session_cookie, create_access_token(world.lead.id))
    assert 'href="/crew/review"' in _menu((await client.get("/home")).text)


async def test_crew_clashes_page_lists_standing_duplicates_and_clashes(client, db, world, auth):
    starts = NOW + timedelta(days=4)
    async def event(name, lead):
        made = await client.post(f"{API}/events", headers=auth(world.president), json={
            "name": name, "starts_at": starts.isoformat(), "ends_at": (starts + timedelta(hours=2)).isoformat(),
            "lead_id": str(lead.id)})
        return made.json()["id"]
    one, two = await event("Open Mic", world.lead), await event("Quiz Night", world.lead2)
    vik = str(world.volunteer.id)
    async def add(event_id, title, **extra):
        made = await client.post(f"{API}/events/{event_id}/tasks", headers=auth(world.president),
                                 json={"title": title, "assignee_id": vik, **extra})
        assert made.status_code == 201, made.text
    await add(one, "Book the DJ")
    await add(one, "Book DJ", override_reason="Two DJs this year")           # near duplicate, overridden
    await add(two, "Run the buzzer", override_reason="He can do both")        # same time, another event

    client.cookies.set(get_settings().session_cookie, create_access_token(world.president.id))
    page = (await client.get("/crew/review")).text
    assert "Near duplicate" in page and "Two events at once" in page and "Vik Volunteer" in page
    assert "Assigned anyway" in page and "Two DJs this year" in page and "He can do both" in page
    # A pair is listed once, not once from each side.
    standing = page[:page.index("Assigned anyway")]
    assert standing.count("Near duplicate</span>") == 1
    # An event lead sees only the events they lead; others are refused.
    client.cookies.set(get_settings().session_cookie, create_access_token(world.lead2.id))
    theirs = (await client.get("/crew/review")).text
    # (the clash message names the other duty, but that task has no row of its own here)
    assert "<td>Run the buzzer" in theirs and "<td>Book DJ" not in theirs and "Assigned anyway" not in theirs
    for other in (world.member, world.volunteer, world.treasurer):
        client.cookies.set(get_settings().session_cookie, create_access_token(other.id))
        assert (await client.get("/crew/review")).status_code == 403
    # Once the duplicate is done, it is no longer standing.
    await db.execute(text("UPDATE crew_tasks SET status = 'done', completed_at = now() WHERE title = 'Book DJ'"))
    await db.commit()
    client.cookies.set(get_settings().session_cookie, create_access_token(world.president.id))
    after = (await client.get("/crew/review")).text
    assert "Near duplicate</span>" not in after[:after.index("Assigned anyway")]      # the record of the override stays


# --- Module detection, the Me page layout, and the door's confirmation box ---------------------


async def test_tables_added_while_the_app_is_running_are_noticed_without_a_restart(db, monkeypatch):
    modules.forget()
    everything = await modules.load(db)
    assert {"memberships", "announcements", "certificates", "handover", "profile"} <= everything
    # Pretend the app first looked before the database was migrated.
    monkeypatch.setattr(modules, "_tables", frozenset({"checkin"}))
    assert "memberships" not in await modules.load(db)                 # looked a moment ago: not asked again yet
    monkeypatch.setattr(modules, "_looked_at", modules._looked_at - modules.RECHECK_SECONDS - 1)
    assert "memberships" in await modules.load(db)                     # looked again, and found them
    # With every module present there is nothing left to look for.
    calls = []
    real = db.execute
    async def counting(*args, **kwargs):
        calls.append(1)
        return await real(*args, **kwargs)
    monkeypatch.setattr(db, "execute", counting)
    monkeypatch.setattr(modules, "_looked_at", 0.0)
    await modules.load(db)
    assert calls == []


async def test_profile_page_lays_its_fields_out_in_rows(client, world, monkeypatch):
    client.cookies.set(get_settings().session_cookie, create_access_token(world.member.id))
    page = (await client.get("/profile")).text
    assert page.count('class="form-row"') == 2 and 'class="upi-row"' in page
    for field in ("full_name", "display_name", "phone", "department", "academic_year", "upi_id"):
        assert f'name="{field}"' in page
    assert "not switched on" not in page
    # Before its tables exist the page still works, with the notice above the form, not under the button.
    await client.get("/home")
    monkeypatch.setattr(modules, "_tables", frozenset({"checkin"}))
    monkeypatch.setattr(modules, "_looked_at", __import__("time").monotonic())
    bare = (await client.get("/profile")).text
    assert 'name="full_name"' in bare and 'name="phone"' not in bare
    assert bare.index("not switched on") < bare.index('id="profile-form"')
