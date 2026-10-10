"""The shared frontend: landing page, sign-in, home hub, navigation and the design system."""

import re
from decimal import Decimal
from pathlib import Path

from app.core.config import get_settings
from app.core.security import create_access_token
from app.services import ledger_service, modules
from app.web.templating import navigation

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def _sign_in(client, user) -> None:
    client.cookies.set(get_settings().session_cookie, create_access_token(user.id))


async def test_landing_page_for_signed_out_visitors(client):
    page = await client.get("/")
    assert page.status_code == 200
    html = page.text
    assert "Who does what? WhoDoo knows." in html and "Twelve tools." in html
    assert html.count('class="slice"') == 9                      # the sliced title animation
    assert 'href="/login"' in html and "Enter WhoDoo" in html
    # Tools that exist in this app link to them (through sign-in); the rest say so honestly.
    assert 'href="/login?next=/store"' in html and 'href="/login?next=/my-tasks"' in html
    assert 'href="/events"' in html and "Browse events" in html   # events are public: no sign-in on the way
    # The test database has every module's tables, so all twelve tools are live.
    assert html.count("Live — open it") == 12 and "Coming soon" not in html
    for href in ("/memberships", "/tickets", "/check-in", "/announcements", "/certificates", "/handover"):
        assert f'href="/login?next={href}"' in html
    old = await client.get("/welcome")
    assert old.status_code == 308 and old.headers["location"] == "/"


async def test_landing_is_the_front_page_even_when_signed_in(client, world):
    _sign_in(client, world.treasurer)
    front = await client.get("/")
    assert front.status_code == 200
    assert "Twelve tools." in front.text and "Hey Tara." not in front.text
    assert 'href="/home"' in front.text and "Open my WhoDoo" in front.text   # the way in to their own pages
    assert 'href="/store"' in front.text                                        # live tools link straight through

    # Their own home lives at /home, and needs a sign-in.
    assert "Hey Tara." in (await client.get("/home")).text
    client.cookies.clear()
    signed_out = await client.get("/home")
    assert signed_out.status_code == 303 and signed_out.headers["location"] == "/dev/login?next=/home"


async def test_design_assets_exist_and_are_referenced(client):
    html = (await client.get("/")).text
    for path in re.findall(r'(?:src|href)="(/static/[^"]+)"', html):
        assert (await client.get(path)).status_code == 200, path
    css = (STATIC / "styles.css").read_text()
    for token in ("--ink: #080808", "--cream: #f4efdf", "--butter: #f4f068", "--coral: #f04b37", "--mint: #8de8bd", "--cobalt: #2858bd"):
        assert token in css                                       # the v0 palette, unchanged
    assert "prefers-reduced-motion: reduce" in css
    assert (await client.get("/static/brand/hero-bg.webp")).status_code == 200


async def test_login_route_leads_to_the_demo_sign_in(client, world):
    response = await client.get("/login", params={"next": "/store"})
    assert response.status_code == 303 and response.headers["location"] == "/dev/login?next=/store"
    for unsafe in ("//evil.example", "https://evil.example", "/"):
        response = await client.get("/login", params={"next": unsafe})
        assert response.headers["location"] == "/dev/login?next=/home"   # after sign-in: their home, nowhere else
    assert (await client.get("/login")).headers["location"] == "/dev/login?next=/home"

    signed_in = await client.post("/dev/login", data={"user_id": str(world.member.id)})
    assert signed_in.status_code == 303 and signed_in.headers["location"] == "/home"
    assert (await client.get("/whoami")).json() == {"id": str(world.member.id), "name": "Mia Member"}
    out = await client.get("/logout")
    assert out.headers["location"] == "/login"
    assert (await client.get("/whoami")).json() == {"id": None, "name": None}

    page = (await client.get("/dev/login", params={"next": "/store"})).text
    assert "Enter WhoDoo" in page and "Demo sign-in" in page
    assert 'aria-label="Sign in as Tara Treasurer, Treasurer"' in page
    assert page.count('name="user_id"') == 7 and 'name="next" value="/store"' in page


async def test_home_shows_live_figures_for_the_person(client, db, world, auth):
    await ledger_service.record_income(
        db, club_id=world.club.id, source_module="membership", amount=Decimal("5000"), created_by=world.treasurer.id
    )
    await db.commit()
    await client.post("/api/v1/merch/products", headers=auth(world.treasurer), json={
        "name": "Sticker", "base_price": "50", "member_price": "40",
        "variants": [{"size": "One", "color": "White", "sku": "STK-1", "stock_quantity": 5}]})

    _sign_in(client, world.treasurer)
    home = (await client.get("/home")).text
    assert "Hey Tara." in home and "Test Club" in home
    assert "₹5,000" in home and "club balance" in home          # from the ledger
    assert "1</div>" in home and "product on sale" in home      # from the catalogue
    assert "to approve" in home

    # A volunteer gets no finance or approvals tiles.
    _sign_in(client, world.volunteer)
    home = (await client.get("/home")).text
    assert "Hey Vik." in home and "club balance" not in home and "to approve" not in home
    assert "open tasks for you" in home and "upcoming events" in home


async def test_landing_page_says_so_when_a_module_has_no_tables(client, monkeypatch):
    await client.get("/")
    monkeypatch.setattr(modules, "_tables", frozenset({"announcements"}))
    html = (await client.get("/")).text
    assert html.count("Live — open it") == 7 and html.count("Coming soon") == 5
    for name in ("MemberCore", "FairSeat", "GateSync", "CertiDoo"):
        card = html[html.index(name) - 600: html.index(name) + 400]
        assert "<article" in card and "Coming soon" in card
    for soon in ("Memberships", "Tickets", "Check-in", "Certificates", "Handover"):
        assert f'aria-disabled="true">{soon}' in html
    assert (await client.get("/api/v1/certificates/verify/ABCDEFGHIJKLMNOP")).status_code == 404


def test_navigation_is_role_aware(monkeypatch):
    monkeypatch.setattr(modules, "_tables", frozenset())   # a database with none of the optional modules

    class Person:
        def __init__(self, role): self.role = role

    def links(role, path="/"):
        nav = navigation(Person(role), path)
        return {section["title"]: [link["label"] for link in section["links"]] for section in nav["sections"]}

    assert links("member") == {
        "Events": ["Events", "My tasks", "Public page"], "Expenses": ["My expenses", "Submit expense"], "Store": ["Store", "My orders"]}
    assert links("event_lead")["Expenses"] == ["My expenses", "Submit expense", "Approvals"]
    assert "Finance" not in links("event_lead")
    assert links("treasurer")["Finance"] == ["Dashboard", "Ledger"]
    assert links("president")["Store"] == ["Store", "My orders", "Merch admin", "Merch orders"]

    def active(role, path):
        nav = navigation(Person(role), path)
        return nav["current"]["title"], next(l["label"] for l in nav["current"]["links"] if l["active"])

    assert active("treasurer", "/treasurer/ledger") == ("Finance", "Ledger")
    assert active("member", "/expenses/new") == ("Expenses", "Submit expense")
    assert active("member", "/expenses/0b3d-some-claim") == ("Expenses", "My expenses")
    assert active("member", "/checkout/abc") == ("Store", "My orders")
    assert active("member", "/events/abc/edit") == ("Events", "Events")
    assert navigation(Person("member"), "/home")["current"] is None
    assert navigation(None, "/")["sections"] == []
    assert navigation(Person("member"), "/")["coming_soon"] == (
        "Memberships", "Tickets", "Check-in", "Announcements", "Certificates", "Handover")

    monkeypatch.setattr(modules, "_tables", frozenset(modules.MODULES))   # every module migrated
    assert links("member")["Tickets"] == ["Book tickets", "My tickets"]              # the door is for door staff
    assert links("volunteer")["Tickets"] == ["Book tickets", "My tickets", "Check-in"]
    assert links("member")["Club"] == ["Memberships", "Announcements", "Certificates", "Handover"]
    assert active("volunteer", "/check-in/some-event") == ("Tickets", "Check-in")
    assert navigation(Person("member"), "/")["coming_soon"] == ()


async def test_shell_marks_the_current_section_and_hides_what_you_cannot_use(client, world):
    _sign_in(client, world.volunteer)
    page = (await client.get("/expenses/new")).text
    assert re.search(r'<a href="/expenses" class="active"\s+aria-current="true">Expenses</a>', page)
    assert re.search(r'<a href="/expenses/new" class="active" aria-current="page">Submit expense</a>', page)
    assert 'href="/treasurer"' not in page and 'href="/admin/merch"' not in page and "Approvals" not in page
    for href in ("/memberships", "/tickets", "/check-in", "/announcements", "/certificates", "/handover"):
        assert f'href="{href}"' in page                           # modules whose tables exist are linked
    assert 'aria-disabled="true"' not in page

    _sign_in(client, world.treasurer)
    page = (await client.get("/treasurer")).text
    assert 'href="/treasurer/ledger"' in page and 'href="/admin/merch/orders"' in page and 'href="/expenses/approvals"' in page


async def test_error_pages_keep_the_shell_and_show_the_status(client, world):
    _sign_in(client, world.volunteer)
    denied = await client.get("/treasurer")
    assert denied.status_code == 403
    assert "403 — Forbidden" in denied.text and "Vik Volunteer" in denied.text and 'href="/expenses"' in denied.text
    missing = await client.get("/no-such-page")
    assert missing.status_code == 404 and "404 — Not found" in missing.text


async def test_actions_use_the_in_page_dialog_not_browser_popups():
    for script in ("app.js", "events.js"):
        source = (STATIC / script).read_text()
        assert "window.confirm(" not in source and "window.prompt(" not in source and "alert(" not in source
    assert "function askDialog" in (STATIC / "app.js").read_text()
