from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.core.config import get_settings
from app.core.dependencies import CurrentUser
from app.services import modules, payment_service

APP_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=APP_DIR / "templates")


def inr(value) -> str:
    """Format as rupees with Indian digit grouping: 1234567.5 -> ₹12,34,567.50"""
    if value is None:
        return "—"
    amount = Decimal(str(value)).quantize(Decimal("0.01"))
    sign = "-" if amount < 0 else ""
    whole, paise = f"{abs(amount):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{sign}₹{whole}.{paise}"


def nice_date(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.astimezone().strftime("%d %b %Y, %H:%M")
    if isinstance(value, date):
        return value.strftime("%d %b %Y")
    return str(value)


def label(value) -> str:
    return str(value).replace("_", " ").capitalize() if value else "—"


def static(path: str) -> str:
    """The address of a file in app/static, tagged with when it last changed. A browser keeps
    stylesheets and scripts until their address changes, so without the tag an edit to styles.css
    is not seen until someone forces a reload."""
    try:
        version = int((APP_DIR / "static" / path).stat().st_mtime)
    except OSError:
        version = 0
    return f"/static/{path}?v={version}"


# A picture for every product: the one its admin gave it, or else an illustration chosen from its
# name, so nothing in the store is ever shown as a blank box.
MERCH_ART = (
    (("hoodie", "sweatshirt", "jacket"), "hoodie"),
    (("t-shirt", "tshirt", "tee", "shirt", "jersey"), "tshirt"),
    (("mug", "cup", "bottle", "flask"), "mug"),
    (("cap", "hat", "beanie"), "cap"),
    (("bag", "tote", "backpack"), "bag"),
    (("sticker", "badge", "pin", "patch"), "sticker"),
)


def product_image(product) -> str:
    if getattr(product, "image_url", None):
        return product.image_url
    name = (getattr(product, "name", "") or "").lower()
    art = next((picture for words, picture in MERCH_ART if any(word in name for word in words)), "item")
    # A photograph where the club has supplied one (app/static/merch/<name>.jpeg), else the drawing.
    photo = f"merch/{art}.jpeg"
    return static(photo if (APP_DIR / "static" / photo).exists() else f"merch/{art}.svg")


templates.env.globals["static"] = static
templates.env.globals["product_image"] = product_image
templates.env.filters["inr"] = inr
templates.env.filters["nice_date"] = nice_date
templates.env.filters["label"] = label


# --- Navigation ---------------------------------------------------------------------
# One description of the app's sections, used by the top bar, the sub-navigation and the
# menu. Each link lists the roles that may see it (None = everyone signed in); the pages
# themselves enforce access, this only decides what is offered.

FINANCE_ROLES = ("treasurer", "president")
APPROVER_ROLES = ("event_lead", "treasurer", "president")
ORGANISER_ROLES = ("event_lead", "president")

DOOR_STAFF = ("volunteer", "event_lead", "treasurer", "president")

# (label, path, roles, module[, menu heading]). A link with a module is offered only where that
# module's tables exist (app/services/modules.py); until then its label is listed as not here yet.
# The optional fifth item files the link under a heading of its own in the three-bar menu.
SECTIONS = (
    ("Events", (
        ("Events", "/events", None, None),
        ("My tasks", "/my-tasks", None, None),
        ("Register for events", "/explore", None, None),
        ("Crew clashes", "/crew/review", ORGANISER_ROLES, None),
    )),
    ("Tickets", (
        ("Book tickets", "/tickets", None, "tickets"),
        ("My tickets", "/tickets/mine", None, "tickets"),
        ("Check-in", "/check-in", DOOR_STAFF, "checkin"),
    )),
    ("Club", (
        ("Memberships", "/memberships", None, "memberships"),
        ("Announcements", "/announcements", None, "announcements"),
        ("Certificates", "/certificates", None, "certificates"),
        ("Handover", "/handover", None, "handover"),
    )),
    ("Me", (
        ("Profile", "/profile", None, None),
    )),
    ("Expenses", (
        ("My expenses", "/expenses", None, None),
        ("Submit expense", "/expenses/new", None, None),
        ("Approvals", "/expenses/approvals", APPROVER_ROLES, None),
    )),
    ("Store", (
        ("Store", "/store", None, None),
        ("My orders", "/orders", None, None),
        # Shown under their own "Merchandise" heading in the menu: running the shop is not shopping.
        ("Merch admin", "/admin/merch", FINANCE_ROLES, None, "Merchandise"),
        ("Merch orders", "/admin/merch/orders", FINANCE_ROLES, None, "Merchandise"),
    )),
    ("Finance", (
        ("Dashboard", "/treasurer", FINANCE_ROLES, None),
        ("Ledger", "/treasurer/ledger", FINANCE_ROLES, None),
    )),
)
# Pages that belong to a link without sharing its path.
ALIASES = {"/checkout": "/orders"}


def coming_soon() -> tuple[str, ...]:
    """Modules whose tables are not in this database yet."""
    found = modules.available()
    return tuple(label for name, (label, _) in modules.MODULES.items()
                 if name not in found and name not in modules.UNLISTED)


def navigation(user: CurrentUser | None, path: str) -> dict:
    """Sections and links this user may see, with the one matching `path` marked active."""
    if user is None:
        return {"sections": [], "current": None, "coming_soon": coming_soon()}
    for prefix, target in ALIASES.items():
        if path.startswith(prefix):
            path = target

    found = modules.available()
    sections = []
    for title, links in SECTIONS:
        visible = [
            {"label": label, "href": href, "active": False, "group": group[0] if group else title}
            for label, href, roles, module, *group in links
            if (roles is None or user.role in roles) and (module is None or module in found)
        ]
        if visible:
            sections.append({"title": title, "href": visible[0]["href"], "links": visible, "active": False})

    # The active link is the one whose path is the longest prefix of the current page.
    best = None
    for section in sections:
        for link in section["links"]:
            href = link["href"]
            if (path == href or path.startswith(href + "/")) and (best is None or len(href) > len(best[1]["href"])):
                best = (section, link)
    if best:
        best[0]["active"] = best[1]["active"] = True
    return {"sections": sections, "current": best[0] if best else None, "coming_soon": coming_soon()}


def render(request: Request, name: str, user: CurrentUser | None = None, status_code: int = 200, **context):
    settings = get_settings()
    context.update(
        user=user,
        demo_auth=settings.demo_auth,
        mock_payments=payment_service.get_provider().is_mock,
        nav=navigation(user, request.url.path),
        path=request.url.path,
        status_code=status_code,
    )
    return templates.TemplateResponse(request, name, context, status_code=status_code)
