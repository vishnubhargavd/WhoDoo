"""Pages for Batch A (Member 2): profile and UPI, notifications, and the Razorpay return.

Like the other pages they only read; every change is a fetch() to /api/v1.
"""

import re
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.dependencies import CurrentUser, get_current_user, get_db
from app.services import event_insights, payout_service, profile_service, reminder_service
from app.services.razorpay_provider import PAYMENT_LINK_ID
from app.web.templating import render, templates

router = APIRouter(include_in_schema=False)

# Seat level for a category on the booking page: ("available" | "low" | "sold_out", message).
templates.env.globals["ticket_level"] = event_insights.level


@router.get("/profile")
async def profile_page(request: Request, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    provider = payout_service.get_payout_provider()
    return render(request, "profile.html", user, profile=await profile_service.get_profile(db, user),
                  payout_provider=provider.name if provider else None,
                  payout_mock=bool(provider and provider.is_mock))


@router.get("/notifications")
async def notifications_page(request: Request, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    reminders = await reminder_service.list_for(db, user)
    await db.commit()
    return render(request, "notifications.html", user, reminders=reminders)


@router.get("/payments/razorpay/return")
async def razorpay_return(next: str = "/home", razorpay_payment_link_id: str = ""):
    """Razorpay sends the payer here after a payment link. Nothing is recorded here: the page we
    pass them on to asks Razorpay itself whether the link is paid."""
    target = next
    base = get_settings().app_base_url.rstrip("/")
    if target.startswith(base + "/"):
        target = target[len(base):]
    parts = urlsplit(target)
    # Same-site paths only: never an open redirect.
    if parts.scheme or parts.netloc or not target.startswith("/") or target.startswith("//") or "\\" in target:
        target = "/home"
    link = razorpay_payment_link_id if PAYMENT_LINK_ID.match(razorpay_payment_link_id or "") else ""
    target = target.replace("{CHECKOUT_SESSION_ID}", link)
    if not link:
        target = re.sub(r"[?&]session_id=(?=&|$)", "", target)
    return RedirectResponse(target, status_code=303)
