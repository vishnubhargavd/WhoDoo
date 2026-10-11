"""The shared frontend: landing page, sign-in, home hub, navigation and the design system."""

import re
import time
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
    assert "Who does what? WhoDoo knows." in html and "What's on." in html and "Twelve tools." not in html
    assert html.count('class="slice"') == 9                      # the sliced title animation
    assert 'href="/login"' in html and "Enter WhoDoo" in html
    # The front page is the events calendar: a month grid drawn from the events embedded in the page.
    assert 'href="/events"' in html and "Browse events" in html   # events are public: no sign-in on the way
    assert "data-calendar" in html and 'class="cal-data"' in html and "/static/calendar.js" in html
    assert 'href="#calendar"' in html and "See the calendar" in html
    # A visitor's menu: the public places, and sign-in.
    for href in ("/events", "/#calendar", "/verify", "/login"):
        assert f'href="{href}"' in html
    old = await client.get("/welcome")
    assert old.status_code == 308 and old.headers["location"] == "/"


async def test_landing_is_the_front_page_even_when_signed_in(client, world):
    _sign_in(client, world.treasurer)
    front = await client.get("/")
    assert front.status_code == 200
    assert "What's on." in front.text and "Hey Tara." not in front.text
    assert 'href="/home"' in front.text and "Open my WhoDoo" in front.text   # the way in to their own pages
    assert 'href="/store"' in front.text                                        # live tools link straight through

    # Their own home lives at /home, and needs a sign-in.
    assert "Hey Tara." in (await client.get("/home")).text
    client.cookies.clear()
    signed_out = await client.get("/home")
    assert signed_out.status_code == 303 and signed_out.headers["location"] == "/login?next=/home"


async def test_design_assets_exist_and_are_referenced(client):
    html = (await client.get("/")).text
    for path in re.findall(r'(?:src|href)="(/static/[^"]+)"', html):
        assert (await client.get(path)).status_code == 200, path
    css = (STATIC / "styles.css").read_text()
    for token in ("--ink: #080808", "--cream: #f4efdf", "--butter: #f4f068", "--coral: #f04b37", "--mint: #8de8bd", "--cobalt: #2858bd"):
        assert token in css                                       # the v0 palette, unchanged
    assert "prefers-reduced-motion: reduce" in css
    assert (await client.get("/static/brand/hero-bg.webp")).status_code == 200


async def test_login_page_is_the_club_tree_and_the_old_picker_is_gone(client, world):
    page = await client.get("/login", params={"next": "/store"})
    assert page.status_code == 200 and "Enter WhoDoo" in page.text
    html = page.text
    # President at the top, then down the ranks; every person is a node.
    order = [html.index(label) for label in ("<strong>President</strong>", "<strong>Treasurer</strong>",
                                             "<strong>Event leads</strong>", "<strong>Members</strong>", "<strong>Volunteers</strong>")]
    assert order == sorted(order)
    assert html.count('class="tree-node secondary"') == 7 and 'aria-label="Sign in as Tara Treasurer, Treasurer"' in html
    assert 'id="login-box"' in html and 'name="username"' in html and 'type="password"' in html
    assert "@test.example" not in html and 'next = "/store"' in html           # names and roles only, no emails
    # The passwordless picker no longer exists: its address leads here, and it accepts nothing.
    old = await client.get("/dev/login", params={"next": "/store"})
    assert old.status_code == 303 and old.headers["location"] == "/login?next=/store"
    assert (await client.post("/dev/login", data={"user_id": str(world.member.id)})).status_code in (404, 405)
    for unsafe in ("//evil.example", "https://evil.example", "/"):
        assert 'next = "/home"' in (await client.get("/login", params={"next": unsafe})).text
    out = await client.get("/logout")
    assert out.headers["location"] == "/login"

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


async def test_the_menu_says_so_when_a_module_has_no_tables(client, world, monkeypatch):
    await client.get("/")
    monkeypatch.setattr(modules, "_tables", frozenset({"announcements"}))     # a database not migrated yet
    monkeypatch.setattr(modules, "_looked_at", time.monotonic())   # just looked: no re-check yet
    _sign_in(client, world.member)
    html = (await client.get("/home")).text
    for soon in ("Memberships", "Tickets", "Certificates", "Handover"):
        assert f'aria-disabled="true">{soon}' in html
    assert 'href="/announcements"' in html and 'href="/memberships"' not in html
    assert (await client.get("/api/v1/certificates/verify/ABCDEFGHIJKLMNOP")).status_code == 404


def test_navigation_is_role_aware(monkeypatch):
    monkeypatch.setattr(modules, "_tables", frozenset())   # a database with none of the optional modules
    monkeypatch.setattr(modules, "_looked_at", time.monotonic())   # just looked: no re-check yet

    class Person:
        def __init__(self, role): self.role = role

    def links(role, path="/"):
        nav = navigation(Person(role), path)
        return {section["title"]: [link["label"] for link in section["links"]] for section in nav["sections"]}

    assert links("member") == {
        "Events": ["Events", "My tasks", "Book events"], "Me": ["Profile"], "Expenses": ["My expenses", "Submit expense"], "Store": ["Store", "My orders"]}
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
    monkeypatch.setattr(modules, "_looked_at", time.monotonic())   # just looked: no re-check yet
    assert links("member")["Tickets"] == ["Book tickets", "My tickets"]              # only because the tests switch FairSeat on
    assert "Check-in" not in links("member")["Events"]                               # the door is for door staff
    assert links("volunteer")["Events"] == ["Events", "My tasks", "Book events", "Check-in"]
    assert links("member")["Club"] == ["Memberships", "Announcements", "Certificates", "Handover"]
    assert active("volunteer", "/check-in/some-event") == ("Events", "Check-in")
    assert navigation(Person("member"), "/")["coming_soon"] == ()

    # As shipped, FairSeat is off: club members get no ticket booking, and it is not listed as "soon" either.
    monkeypatch.setattr(get_settings(), "fairseat_enabled", False)
    assert "Tickets" not in links("member") and "Tickets" not in links("president")
    assert "Tickets" not in navigation(Person("member"), "/")["coming_soon"]
    assert links("volunteer")["Events"][-1] == "Check-in"                            # the door still works


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
