"""Routes from Member 2's Batch A that this build carries: your profile and UPI id, your
notifications, and the event budget.

Everything is about the signed-in person; no route edits someone else's profile. Each part
answers 404 until its table exists (app/services/modules.py). Razorpay and reimbursement
payouts from the same batch are not included here.
"""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import CurrentUser, get_current_user, get_db
from app.schemas.member2 import (
    BudgetIn,
    BudgetOut,
    PaymentProfileIn,
    PaymentProfileOut,
    ProfileOut,
    ProfileUpdate,
    ReminderOut,
)
from app.services import event_insights, modules, notification_service, profile_service, reminder_service
from app.services.notification_service import NotificationFeed

me = APIRouter(prefix="/me", tags=["Profile"])
events = APIRouter(prefix="/events", tags=["Event budget"])


# --- Profile and UPI ------------------------------------------------------------------------


@me.get("/profile", response_model=ProfileOut)
async def get_profile(user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> ProfileOut:
    return await profile_service.get_profile(db, user)


@me.put("/profile", response_model=ProfileOut)
async def update_profile(
    body: ProfileUpdate, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> ProfileOut:
    """Your own name, display name, phone, department and year. Role, email (your sign-in),
    membership status and expiry cannot be changed here; unknown fields are refused."""
    out = await profile_service.update_profile(db, user, body)
    await db.commit()
    return out


@me.put("/payment-profile", response_model=PaymentProfileOut, dependencies=[Depends(modules.require("profile"))])
async def set_payment_profile(
    body: PaymentProfileIn, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> PaymentProfileOut:
    """Add or replace your UPI id. It is stored encrypted and shown masked. Saving it never
    authorises anyone to debit you."""
    out = await profile_service.set_upi(db, user, body.upi_id)
    await db.commit()
    return out


@me.delete("/payment-profile", status_code=204, dependencies=[Depends(modules.require("profile"))])
async def remove_payment_profile(user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> None:
    await profile_service.remove_upi(db, user)
    await db.commit()


# --- Notifications --------------------------------------------------------------------------


@me.get("/notifications/feed", response_model=NotificationFeed)
async def notification_feed(
    unread_only: bool = False, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> NotificationFeed:
    """Everything the bell shows: renewal reminders and announcements, newest first, with the unread count."""
    found = await notification_service.feed(db, user, unread_only=unread_only)
    await db.commit()   # reminders that have just come due are written as they are read
    return found


@me.get("/notifications", response_model=list[ReminderOut])
async def notifications(
    unread_only: bool = False, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[ReminderOut]:
    """Your membership renewal reminders for the current term."""
    found = await reminder_service.list_for(db, user, unread_only=unread_only)
    await db.commit()
    return found


@me.post("/notifications/{reminder_id}/read", status_code=204, dependencies=[Depends(modules.require("reminders"))])
async def mark_read(
    reminder_id: uuid.UUID, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> None:
    await reminder_service.mark_read(db, user, reminder_id)
    await db.commit()


# --- Event budget ---------------------------------------------------------------------------


@events.get("/{event_id}/budget", response_model=BudgetOut)
async def get_budget(
    event_id: uuid.UUID, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> BudgetOut:
    return await event_insights.budget(db, user, event_id)


@events.put("/{event_id}/budget", response_model=BudgetOut)
async def set_budget(
    event_id: uuid.UUID, body: BudgetIn, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> BudgetOut:
    """The president, the event's lead or the treasurer. Whole paise."""
    out = await event_insights.set_budget(db, user, event_id, body)
    await db.commit()
    return out
