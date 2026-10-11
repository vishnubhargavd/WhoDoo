"""What the notification bell shows: one list drawn from the modules that have something to say.

    membership renewal reminders   (reminder_service, Member 2)
    announcements                  (announcement_service)

Nothing is stored here. Each item says where it leads and how to mark it read, and its read
state belongs to the module it came from. A module whose tables are missing contributes nothing.
"""

from datetime import datetime

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import CurrentUser
from app.services import announcement_service, modules, reminder_service


class NotificationOut(BaseModel):
    id: str                 # "<kind>:<uuid>", unique across kinds
    kind: str               # renewal | announcement
    title: str
    message: str
    created_at: datetime
    href: str               # where clicking it goes
    read: bool
    read_url: str           # POST here to mark it read


class NotificationFeed(BaseModel):
    unread: int
    items: list[NotificationOut]


async def feed(db: AsyncSession, user: CurrentUser, unread_only: bool = False, limit: int = 30) -> NotificationFeed:
    """Newest first. `unread` counts everything unread, not just what fits in `limit`."""
    items: list[NotificationOut] = []
    for reminder in await reminder_service.list_for(db, user):
        items.append(NotificationOut(
            id=f"renewal:{reminder.id}", kind="renewal", title=reminder.title, message=reminder.message,
            created_at=reminder.created_at, href=reminder.renew_url, read=reminder.read,
            read_url=f"/api/v1/me/notifications/{reminder.id}/read",
        ))
    if modules.has("announcements"):
        for post in await announcement_service.feed(db, user):
            who = f"{post.author_name}: " if post.author_name else ""
            text = post.body if len(post.body) <= 160 else post.body[:157].rstrip() + "…"
            items.append(NotificationOut(
                id=f"announcement:{post.id}", kind="announcement", title=post.title, message=who + text,
                created_at=post.created_at, href="/announcements", read=post.read,
                read_url=f"/api/v1/announcements/{post.id}/read",
            ))
    items.sort(key=lambda item: item.created_at, reverse=True)
    unread = sum(not item.read for item in items)
    if unread_only:
        items = [item for item in items if not item.read]
    return NotificationFeed(unread=unread, items=items[:limit])
