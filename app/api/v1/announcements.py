import uuid
from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import CurrentUser, get_current_user, get_db
from app.schemas.announcement import AnnouncementCreate, AnnouncementOut
from app.services import announcement_service

router = APIRouter(prefix="/announcements", tags=["WhoDoo Connect"])


@router.get("", response_model=list[AnnouncementOut])
async def list_club_announcements(
    audience: str | None = None,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[AnnouncementOut]:
    items = await announcement_service.list_announcements(db, user.club_id, audience)
    return [AnnouncementOut.model_validate(item) for item in items]


@router.post("", response_model=AnnouncementOut, status_code=status.HTTP_201_CREATED)
async def post_announcement(
    data: AnnouncementCreate,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AnnouncementOut:
    item = await announcement_service.create_announcement(db, user, data)
    await db.commit()
    return AnnouncementOut.model_validate(item)


@router.delete("/{announcement_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_announcement(
    announcement_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await announcement_service.delete_announcement(db, user, announcement_id)
    await db.commit()
