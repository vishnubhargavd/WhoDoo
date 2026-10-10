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


def render(request: Request, name: str, user: CurrentUser | None = None, status_code: int = 200, **context):
    settings = get_settings()
    context.update(user=user, demo_auth=settings.demo_auth, mock_payments=not settings.use_razorpay)
    return templates.TemplateResponse(request, name, context, status_code=status_code)
