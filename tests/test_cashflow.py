"""The treasurer's cash-flow views, certificate emails, and pages that print and refresh properly."""

import email
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import create_access_token
from app.models.expense import ExpenseClaim
from app.services import ledger_service, modules, payment_service

API = "/api/v1"
NOW = datetime.now(timezone.utc)
RECEIPT = (Path(__file__).resolve().parents[1] / "app" / "static" / "samples" / "receipt_bake_sale_850.png").read_bytes()


@pytest.fixture(autouse=True)
def _fresh():
    modules.forget()
    provider = payment_service.get_provider()
    provider.refunds.clear()
    provider.refund_results.clear()
    for path in Path(get_settings().mail_outbox_dir).glob("*.eml"):
        path.unlink()


def code(response) -> str:
    return response.json()["error"]["code"]


async def claim(client, auth, user, event_id, amount, category):
    upload = await client.post(f"{API}/expenses/ocr", headers=auth(user), files={"file": ("r.png", RECEIPT, "image/png")})
    made = await client.post(f"{API}/expenses", headers=auth(user), json={
        "receipt_id": upload.json()["receipt_id"], "title": f"{category} for the event", "description": "Bought for the event",
        "category": category, "amount": amount, "expense_date": "2026-10-03", "vendor_name": "Shop", "event_id": event_id})
    assert made.status_code == 201, made.text
    return made.json()["id"]


async def paid_event(client, auth, world, price=15000) -> str:
    venue = (await client.get(f"{API}/public/venues?fits=50")).json()[0]["id"]
    made = await client.post(f"{API}/events", headers=auth(world.lead), json={
        "name": "Hack Night", "starts_at": (NOW + timedelta(days=3)).isoformat(),
        "ends_at": (NOW + timedelta(days=3, hours=3)).isoformat(), "registration_mode": "paid",
        "price_paise": price, "max_participants": 50, "venue_id": venue})
    event_id = made.json()["id"]
    await client.post(f"{API}/events/{event_id}/status", json={"status": "published"}, headers=auth(world.lead))
    return event_id


async def register_and_pay(client, event_id, n) -> str:
    made = (await client.post(f"{API}/public/events/{event_id}/register", json={
        "full_name": f"Student {n}", "phone": f"98765{n:05d}", "email": f"student{n}@college.example",
        "student_id": f"22CS{n:04d}"})).json()
    token = made["ticket_url"].rsplit("/", 1)[1]
    await client.post(f"{API}/public/registrations/{token}/checkout")
    paid = await client.post(f"{API}/public/registrations/{token}/mock", json={"outcome": "success"})
    assert paid.json()["outcome"] == "registered", paid.text
    return token


async def test_an_events_cash_flow_adds_up_from_the_ledger(client, db, world, auth):
    event_id = await paid_event(client, auth, world)
    for n in (1, 2, 3):
        await register_and_pay(client, event_id, n)                      # 3 x 150 in
    await client.put(f"{API}/events/{event_id}/budget", json={"budget_paise": 150000}, headers=auth(world.treasurer))

    prizes = await claim(client, auth, world.volunteer, event_id, "850.00", "prizes")      # approved, then paid
    await client.post(f"{API}/expenses/{prizes}/approve", headers=auth(world.lead))
    await client.post(f"{API}/expenses/{prizes}/mark-paid", headers=auth(world.treasurer))
    goodies = await claim(client, auth, world.volunteer, event_id, "200.00", "goodies")    # approved, not paid
    await client.post(f"{API}/expenses/{goodies}/approve", headers=auth(world.lead))
    await claim(client, auth, world.volunteer, event_id, "300.00", "food")                 # still waiting

    flow = (await client.get(f"{API}/ledger/events/{event_id}/cash-flow", headers=auth(world.treasurer))).json()
    money_in, out = flow["money_in"], flow["money_out"]
    assert (Decimal(money_in["registrations"]), money_in["registration_count"], Decimal(money_in["total"])) == (Decimal("450.00"), 3, Decimal("450.00"))
    assert (Decimal(out["paid"]), Decimal(out["committed"]), Decimal(out["pending"])) == (Decimal("850.00"), Decimal("200.00"), Decimal("300.00"))
    lines = {line["category"]: line for line in out["lines"]}
    assert set(lines) == {"prizes", "goodies", "food"}
    assert (Decimal(lines["prizes"]["paid"]), Decimal(lines["goodies"]["committed"]), Decimal(lines["food"]["pending"])) == (
        Decimal("850.00"), Decimal("200.00"), Decimal("300.00"))
    assert Decimal(flow["net"]) == Decimal("-400.00")                      # 450 in, 850 out
    assert Decimal(flow["net_after_commitments"]) == Decimal("-900.00")    # and 500 more on its way out
    budget = flow["budget"]
    assert (budget["budget_paise"], budget["spent_paise"], budget["committed_paise"], budget["remaining_paise"]) == (150000, 85000, 20000, 45000)

    # A refund comes off what was kept.
    registration_id = (await client.get(f"{API}/events/{event_id}/registrations", headers=auth(world.lead))).json()["registrations"][0]["id"]
    await client.post(f"{API}/events/registrations/{registration_id}/cancel", headers=auth(world.lead))
    after = (await client.get(f"{API}/ledger/events/{event_id}/cash-flow", headers=auth(world.treasurer))).json()
    assert (Decimal(after["refunds"]), Decimal(after["net"])) == (Decimal("150.00"), Decimal("-550.00"))

    # The page, for the treasurer and president only.
    client.cookies.set(get_settings().session_cookie, create_access_token(world.treasurer.id))
    page = (await client.get(f"/treasurer/events/{event_id}")).text
    for shown in ("Money in", "Event registrations", "Prizes", "Goodies", "₹850.00", "Budget", "Every entry"):
        assert shown in page, shown
    assert f'href="/treasurer/events/{event_id}"' in (await client.get(f"/events/{event_id}")).text
    client.cookies.clear()
    # The president and the treasurer see every event; a lead sees the events they lead; nobody else.
    for allowed in (world.president, world.treasurer, world.lead):
        assert (await client.get(f"{API}/ledger/events/{event_id}/cash-flow", headers=auth(allowed))).status_code == 200, allowed.full_name
        client.cookies.set(get_settings().session_cookie, create_access_token(allowed.id))
        assert (await client.get(f"/treasurer/events/{event_id}")).status_code == 200, allowed.full_name
        assert f'href="/treasurer/events/{event_id}"' in (await client.get(f"/events/{event_id}")).text, allowed.full_name
    client.cookies.clear()
    for other in (world.lead2, world.volunteer, world.member):
        assert (await client.get(f"{API}/ledger/events/{event_id}/cash-flow", headers=auth(other))).status_code == 403
    assert (await client.get(f"{API}/ledger/events/{uuid.uuid4()}/cash-flow", headers=auth(world.treasurer))).status_code == 404


async def test_the_overview_counts_every_source_once(client, db, world, auth):
    event_id = await paid_event(client, auth, world)
    await register_and_pay(client, event_id, 1)                                       # 150 from an event
    plan = (await client.post(f"{API}/memberships/plans", headers=auth(world.treasurer),
                              json={"name": "Annual", "price_paise": 50000, "validity_days": 365})).json()
    await client.post(f"{API}/memberships/enroll", json={"plan_id": plan["id"]}, headers=auth(world.outsider))
    checkout = (await client.post(f"{API}/memberships/checkout", headers=auth(world.outsider))).json()
    await client.post(f"{API}/memberships/checkout/mock", json={"session_id": checkout["session_id"]}, headers=auth(world.outsider))   # 500 dues
    await ledger_service.record_income(db, club_id=world.club.id, source_module="merchandise", amount=Decimal("1200.00"),
                                       created_by=world.treasurer.id, description="Hoodie order")
    await db.commit()

    overview = (await client.get(f"{API}/ledger/overview", headers=auth(world.president))).json()
    by = {s["key"]: Decimal(s["amount"]) for s in overview["sources"]}
    assert by == {"memberships": Decimal("500.00"), "events": Decimal("150.00"), "merchandise": Decimal("1200.00"), "other": Decimal("0.00")}
    assert Decimal(overview["generated"]) == Decimal("1850.00") == Decimal(overview["net"])
    assert Decimal(overview["merchandise"]["revenue"]) == Decimal("1200.00")
    # It agrees with the ledger's own summary: nothing counted twice.
    summary = (await client.get(f"{API}/ledger/summary", headers=auth(world.treasurer))).json()
    assert Decimal(summary["total_income"]) == Decimal(overview["generated"])

    client.cookies.set(get_settings().session_cookie, create_access_token(world.treasurer.id))
    page = (await client.get("/treasurer")).text
    assert "built-in method" not in page and overview["merchandise"]["items_sold"] == 0      # a number, shown as one
    assert '<div class="label">Items sold</div><div class="value">0</div>' in page
    for shown in ("Money generated", "Event registrations and tickets", "Merchandise", "Cash flow by event", "₹1,850.00",
                  f'href="/treasurer/events/{event_id}"'):
        assert shown in page, shown
    assert (await client.get(f"{API}/ledger/overview", headers=auth(world.lead))).status_code == 403


async def test_prizes_and_goodies_are_expense_categories(client, db, world, auth):
    made = await claim(client, auth, world.volunteer, None, "120.00", "prizes")
    assert await db.scalar(select(ExpenseClaim.category).where(ExpenseClaim.id == uuid.UUID(made))) == "prizes"
    client.cookies.set(get_settings().session_cookie, create_access_token(world.volunteer.id))
    form = (await client.get("/expenses/new")).text
    assert 'value="prizes"' in form and 'value="goodies"' in form


async def test_a_certificate_is_emailed_in_the_sites_colours_when_issued(client, world, auth):
    made = await client.post(f"{API}/certificates", headers=auth(world.president), json={
        "recipient_user_id": str(world.member.id), "title": "Best Newcomer", "kind": "achievement"})
    assert made.status_code == 201
    outbox = sorted(Path(get_settings().mail_outbox_dir).glob("*.eml"))
    assert len(outbox) == 1
    message = email.message_from_bytes(outbox[0].read_bytes())
    assert message["To"] == world.member.email and "Best Newcomer" in message["Subject"]
    parts = {part.get_content_type(): part for part in message.walk()}
    text, html = (parts[kind].get_payload(decode=True).decode() for kind in ("text/plain", "text/html"))
    code_ = made.json()["verify_code"]
    assert f"/verify/{code_}" in text and f"/certificates/{made.json()['id']}" in text and "Priya President" in text
    for colour in ("#f4efdf", "#f4f068", "#080808"):                       # cream, butter, ink
        assert colour in html
    assert "Mia Member" in html and "cid:" in html and "<style" not in html     # inline styles only: mail apps drop stylesheets
    assert parts["image/png"].get_payload(decode=True).startswith(b"\x89PNG")
    # Sending again says plainly that no mail server is set up, instead of pretending.
    again = await client.post(f"{API}/certificates/{made.json()['id']}/email", headers=auth(world.member))
    assert (again.status_code, code(again)) == (409, "email_not_configured")
    assert (await client.post(f"{API}/certificates/{made.json()['id']}/email", headers=auth(world.outsider))).status_code == 404


async def test_pages_fetch_fresh_styles_and_print_as_documents(client, world, auth):
    page = (await client.get("/")).text
    assert "/static/styles.css?v=" in page and "/static/app.js?v=" in page and "/static/calendar.js?v=" in page
    css = (await client.get("/static/styles.css")).text
    # Portrait by default; only the certificate asks for landscape, by name.
    assert "@page { size: A4 portrait" in css and "@page certificate { size: A4 landscape" in css and "page: certificate" in css
    assert ".ticket-sheet" in css and ".screen-only" in css


async def test_every_product_has_a_picture_and_qr_codes_scale_to_their_box(client, db, world, auth):
    from app.services import gatesync_service
    from app.web.templating import product_image

    class Item:
        def __init__(self, name, image_url=None): self.name, self.image_url = name, image_url

    # The hoodie, shirt and mug use the club's own photographs; the rest fall back to drawings.
    assert "merch/hoodie.jpeg" in product_image(Item("Club Hoodie"))
    assert "merch/tshirt.jpeg" in product_image(Item("Fest T-Shirt 2026"))
    assert "merch/mug.jpeg" in product_image(Item("Enamel Mug"))
    assert "merch/cap.svg" in product_image(Item("Club Cap"))
    for name in ("hoodie", "tshirt", "mug"):
        photo = await client.get(f"/static/merch/{name}.jpeg")
        assert photo.status_code == 200 and photo.content[:2] == b"\xff\xd8"          # a real JPEG
    assert "merch/item.svg" in product_image(Item("Mystery Box"))                 # anything else still gets one
    assert product_image(Item("Club Hoodie", "https://example.com/h.png")) == "https://example.com/h.png"
    for name in ("hoodie", "tshirt", "mug", "cap", "bag", "sticker", "item"):
        picture = await client.get(f"/static/merch/{name}.svg")
        assert picture.status_code == 200 and picture.text.startswith("<svg")
    # A QR code carries a viewBox and no fixed pixel size, so a small box shows all of it, not a corner.
    svg = gatesync_service.qr_svg("http://192.168.1.24:8000/verify/ABCDEFGHIJKLMNOP")
    head = svg[:svg.index(">")]
    assert "viewBox=" in head and "width=" not in head and "height=" not in head


async def test_an_announcement_never_fails_on_a_leftover_event_choice(client, world, auth):
    url = f"{API}/announcements"
    # Posting to everyone with an event still selected in the form: the event is simply not used.
    ok = await client.post(url, headers=auth(world.president), json={
        "title": "Welcome", "body": "Hello all.", "audience": "everyone", "event_id": str(world.bake_sale.id)})
    assert ok.status_code == 201, ok.text
    feed = (await client.get(url, headers=auth(world.member))).json()
    assert [(a["title"], a["audience"], a["event_id"]) for a in feed] == [("Welcome", "everyone", None)]
    # An event announcement with no event says so in plain words.
    missing = await client.post(url, headers=auth(world.president), json={"title": "Bring ID", "body": "Please.", "audience": "event"})
    assert missing.status_code == 422
    message = missing.json()["error"]["message"]
    assert "Pick the event this announcement is for." in message and "Value error" not in message
    assert (await client.post(url, headers=auth(world.president), json={
        "title": "Bring ID", "body": "Please.", "audience": "event", "event_id": str(world.bake_sale.id)})).status_code == 201
    client.cookies.set(get_settings().session_cookie, create_access_token(world.president.id))
    page = (await client.get("/announcements")).text
    assert 'id="announce-event" hidden' in page and 'id="announce-audience"' in page


async def test_no_validation_message_starts_with_value_error(client, world, auth):
    """Messages written for people reach them as written, without the library's own prefix."""
    cases = [
        ("post", f"{API}/merch/products", world.treasurer, {"name": "Cap", "base_price": "100.00", "member_price": "150.00"}),
        ("put", f"{API}/me/profile", world.member, {"phone": "12ab"}),
        ("post", f"{API}/ledger/manual-entry", world.treasurer, {"entry_type": "adjustment", "amount": "10.00", "description": "Fix up"}),
        ("post", f"{API}/check-in/manual", world.volunteer, {"event_id": str(world.bake_sale.id)}),
    ]
    for method, url, who, body in cases:
        response = await getattr(client, method)(url, headers=auth(who), json=body)
        assert response.status_code == 422, (url, response.status_code, response.text[:200])
        assert "Value error" not in response.text, url


async def test_every_page_renders_cleanly_for_every_role(client, world, auth, db):
    """A sweep for silly mistakes: no page may crash, or print a Python object where a value belongs."""
    event_id = await paid_event(client, auth, world)
    await register_and_pay(client, event_id, 1)
    pages = ["/", "/home", "/events", "/explore", f"/events/{event_id}", f"/events/{event_id}/edit", "/events/new", "/my-tasks",
             "/crew/review", "/check-in", f"/check-in/{event_id}", "/memberships", "/announcements", "/certificates", "/handover",
             "/profile", "/notifications", "/expenses", "/expenses/new", "/expenses/approvals", "/store", "/orders",
             "/admin/merch", "/admin/merch/orders", "/treasurer", "/treasurer/ledger", f"/treasurer/events/{event_id}",
             f"/e/{event_id}", "/verify", "/login"]
    bad = ("built-in method", "bound method", "<function ", "Traceback", "Undefined", "Internal Server Error", "Value error")
    for person in (world.president, world.treasurer, world.lead, world.lead2, world.volunteer, world.member, world.outsider, None):
        client.cookies.clear()
        if person is not None:
            client.cookies.set(get_settings().session_cookie, create_access_token(person.id))
        for path in pages:
            response = await client.get(path)
            who = person.full_name if person else "a visitor"
            assert response.status_code in (200, 303, 403, 404), (who, path, response.status_code)
            if response.status_code == 200:
                for marker in bad:
                    assert marker not in response.text, (who, path, marker)
    # And the president is never the one turned away.
    client.cookies.set(get_settings().session_cookie, create_access_token(world.president.id))
    for path in pages:
        assert (await client.get(path)).status_code in (200, 303), ("president", path)
