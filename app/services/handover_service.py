"""LegacyVault: handover notes for whoever holds a role next.

A note is written for a role. Officers (event leads, the treasurer, the president) read all
of them; volunteers read the volunteer and everyone notes; members read the everyone notes.
The president can write for any role, the treasurer and event leads for their own role and
for volunteers and everyone. Saving a note keeps the previous text as a version.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import APPROVERS, ROLE_PRESIDENT, ROLE_VOLUNTEER, CurrentUser
from app.core.errors import forbidden, not_found
from app.models.community import HANDOVER_ROLES, HandoverNote, HandoverNoteVersion
from app.models.shared import User
from app.schemas.community import HandoverNoteIn, HandoverNoteOut, HandoverNoteUpdate
from app.services import audit_service


def readable_roles(user: CurrentUser) -> tuple[str, ...]:
    if user.role in APPROVERS:
        return HANDOVER_ROLES
    return ("volunteer", "everyone") if user.role == ROLE_VOLUNTEER else ("everyone",)


def writable_roles(user: CurrentUser) -> tuple[str, ...]:
    if user.role == ROLE_PRESIDENT:
        return HANDOVER_ROLES
    return (user.role, "volunteer", "everyone") if user.role in APPROVERS else ()


async def _out(session: AsyncSession, user: CurrentUser, notes: list[HandoverNote]) -> list[HandoverNoteOut]:
    names = {}
    if notes:
        names = dict((await session.execute(
            select(User.id, User.full_name).where(User.id.in_({note.updated_by for note in notes}))
        )).all())
    can = writable_roles(user)
    return [
        HandoverNoteOut(
            id=note.id, role=note.role, title=note.title, body=note.body, version=note.version,
            updated_by_name=names.get(note.updated_by, ""), updated_at=note.updated_at,
            archived=note.archived_at is not None, can_edit=note.role in can,
        )
        for note in notes
    ]


async def list_notes(session: AsyncSession, user: CurrentUser, include_archived: bool = False) -> list[HandoverNoteOut]:
    stmt = select(HandoverNote).where(
        HandoverNote.club_id == user.club_id, HandoverNote.role.in_(readable_roles(user))
    )
    if not (include_archived and user.role in APPROVERS):
        stmt = stmt.where(HandoverNote.archived_at.is_(None))
    return await _out(session, user, list(await session.scalars(stmt.order_by(HandoverNote.role, HandoverNote.title))))


async def _editable(session: AsyncSession, user: CurrentUser, note_id: uuid.UUID) -> HandoverNote:
    note = await session.scalar(
        select(HandoverNote).where(HandoverNote.id == note_id, HandoverNote.club_id == user.club_id).with_for_update()
    )
    if note is None or note.role not in readable_roles(user):
        raise not_found("Handover note")
    if note.role not in writable_roles(user):
        raise forbidden("You cannot change handover notes for this role")
    return note


async def create(session: AsyncSession, user: CurrentUser, data: HandoverNoteIn) -> HandoverNoteOut:
    if data.role not in writable_roles(user):
        raise forbidden("You cannot write handover notes for this role")
    note = HandoverNote(id=uuid.uuid4(), club_id=user.club_id, created_by=user.id, updated_by=user.id,
                        version=1, **data.model_dump())
    session.add(note)
    await session.flush()
    audit_service.record(
        session, club_id=user.club_id, actor_id=user.id, action="legacyvault.note_created",
        entity_type="handover_note", entity_id=note.id, details={"role": note.role, "title": note.title},
    )
    return (await _out(session, user, [note]))[0]


async def update(session: AsyncSession, user: CurrentUser, note_id: uuid.UUID, data: HandoverNoteUpdate) -> HandoverNoteOut:
    note = await _editable(session, user, note_id)
    if (note.title, note.body) != (data.title, data.body):
        session.add(HandoverNoteVersion(
            note_id=note.id, version=note.version, title=note.title, body=note.body, saved_by=note.updated_by,
        ))
        note.title, note.body = data.title, data.body
        note.version += 1
        note.updated_by, note.updated_at = user.id, datetime.now(timezone.utc)
        await session.flush()
    return (await _out(session, user, [note]))[0]


async def set_archived(session: AsyncSession, user: CurrentUser, note_id: uuid.UUID, archived: bool) -> HandoverNoteOut:
    note = await _editable(session, user, note_id)
    note.archived_at = datetime.now(timezone.utc) if archived else None
    await session.flush()
    audit_service.record(
        session, club_id=user.club_id, actor_id=user.id,
        action="legacyvault.note_archived" if archived else "legacyvault.note_restored",
        entity_type="handover_note", entity_id=note.id, details=None,
    )
    return (await _out(session, user, [note]))[0]


async def versions(session: AsyncSession, user: CurrentUser, note_id: uuid.UUID) -> list[dict]:
    """Earlier versions of a note, newest first, for anyone who may read it."""
    note = await session.get(HandoverNote, note_id)
    if note is None or note.club_id != user.club_id or note.role not in readable_roles(user):
        raise not_found("Handover note")
    rows = (await session.execute(
        select(HandoverNoteVersion, User.full_name)
        .join(User, User.id == HandoverNoteVersion.saved_by)
        .where(HandoverNoteVersion.note_id == note.id)
        .order_by(HandoverNoteVersion.version.desc())
    )).all()
    return [{"version": v.version, "title": v.title, "body": v.body, "saved_by_name": name, "saved_at": v.saved_at}
            for v, name in rows]
