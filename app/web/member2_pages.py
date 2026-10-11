"""Pages from Member 2's Batch A: your profile and UPI id, and the full notifications list.

Like the other pages they only read; every change is a fetch() to /api/v1.
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import CurrentUser, get_current_user, get_db
from app.core.errors import forbidden
from app.services import assignment_review, event_service, notification_service, profile_service
from app.web.templating import render

router = APIRouter(include_in_schema=False)


@router.get("/profile")
async def profile_page(request: Request, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return render(request, "profile.html", user, profile=await profile_service.get_profile(db, user))


@router.get("/notifications")
async def notifications_page(request: Request, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Everything the bell has shown, read or not."""
    found = await notification_service.feed(db, user, limit=200)
    await db.commit()
    return render(request, "notifications.html", user, feed=found)


@router.get("/crew/review")
async def crew_review_page(request: Request, user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Volunteer duplicates and clashes across the club's open events, for whoever hands out tasks.
    The president sees every event; an event lead sees the events they lead."""
    if user.role not in ("president", "event_lead"):
        raise forbidden("Only the president and event leads review task clashes")
    mine_only = user.id if user.role == "event_lead" else None
    return render(
        request, "crew_review.html", user,
        flagged=await assignment_review.scan(db, user.club_id, lead_id=mine_only),
        event_clashes=await event_service.standing_event_clashes(db, user.club_id, lead_id=mine_only),
        overrides=await assignment_review.overrides(db, user.club_id) if user.role == "president" else [],
    )
