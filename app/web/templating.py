from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.core.config import get_settings
from app.core.dependencies import CurrentUser

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


templates.env.filters["inr"] = inr
templates.env.filters["nice_date"] = nice_date
templates.env.filters["label"] = label


# --- Navigation ---------------------------------------------------------------------
# One description of the app's sections, used by the top bar, the sub-navigation and the
# menu. Each link lists the roles that may see it (None = everyone signed in); the pages
# themselves enforce access, this only decides what is offered.

FINANCE_ROLES = ("treasurer", "president")
APPROVER_ROLES = ("event_lead", "treasurer", "president")

SECTIONS = (
    ("Events", (
        ("Events", "/events", None),
        ("My tasks", "/my-tasks", None),
    )),
    ("Expenses", (
        ("My expenses", "/expenses", None),
        ("Submit expense", "/expenses/new", None),
        ("Approvals", "/expenses/approvals", APPROVER_ROLES),
    )),
    ("Store", (
        ("Store", "/store", None),
        ("My orders", "/orders", None),
        ("Merch admin", "/admin/merch", FINANCE_ROLES),
        ("Merch orders", "/admin/merch/orders", FINANCE_ROLES),
    )),
    ("Finance", (
        ("Dashboard", "/treasurer", FINANCE_ROLES),
        ("Ledger", "/treasurer/ledger", FINANCE_ROLES),
    )),
)
# Pages that belong to a link without sharing its path.
ALIASES = {"/checkout": "/orders"}
# Modules owned by other team members that are not in this application yet.
COMING_SOON = ("Memberships", "Tickets", "Check-in", "Announcements", "Certificates")


def navigation(user: CurrentUser | None, path: str) -> dict:
    """Sections and links this user may see, with the one matching `path` marked active."""
    if user is None:
        return {"sections": [], "current": None, "coming_soon": COMING_SOON}
    for prefix, target in ALIASES.items():
        if path.startswith(prefix):
            path = target

    sections = []
    for title, links in SECTIONS:
        visible = [
            {"label": label, "href": href, "active": False}
            for label, href, roles in links
            if roles is None or user.role in roles
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
    return {"sections": sections, "current": best[0] if best else None, "coming_soon": COMING_SOON}


def render(request: Request, name: str, user: CurrentUser | None = None, status_code: int = 200, **context):
    settings = get_settings()
    context.update(
        user=user,
        demo_auth=settings.demo_auth,
        mock_payments=not settings.use_razorpay,
        nav=navigation(user, request.url.path),
        path=request.url.path,
        status_code=status_code,
    )
    return templates.TemplateResponse(request, name, context, status_code=status_code)
