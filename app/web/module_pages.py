"""Pages for Memberships, Tickets, Check-in, Announcements, Certificates and Handover.

Like the other pages they only read; every action is a fetch() to /api/v1. Each page
answers 404 until its module's tables exist (app/services/modules.py).
"""

import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.dependencies import CurrentUser, get_current_user, get_db
from app.core.errors import AppError
from app.models.shared import Event, User
from app.services import (
    announcement_service,
    certificate_service,
    fairseat_payments,
    fairseat_service,
    gatesync_service,
    handover_service,
    membership_service,
    modules,
    ticket_service,
)
from app.web.templating import render

router = APIRouter(include_in_schema=False)
Me = Depends(get_current_user)
Db = Depends(get_db)


def needs(module: str):
    return [Depends(modules.require(module))]


async def _club_events(db: AsyncSession, user: CurrentUser) -> list[Event]:
    return list(await db.scalars(
        select(Event).where(Event.club_id == user.club_id, Event.status != "draft")
        .order_by(Event.starts_at.desc().nulls_last(), Event.name)
    ))


# --- MemberCore -------------------------------------------------------------------------


@router.get("/memberships", dependencies=needs("memberships"))
async def memberships(request: Request, user: CurrentUser = Me, db: AsyncSession = Db):
    await membership_service.expire_lapsed(db, user.club_id)
    await db.commit()
    officer = user.is_finance_admin
    return render(
        request, "memberships.html", user,
        mine=await membership_service.my_membership(db, user),
        plans=await membership_service.list_plans(db, user, include_hidden=officer),
        members=await membership_service.list_members(db, user) if officer else [],
        officer=officer,
    )


@router.get("/memberships/return", dependencies=needs("memberships"))
async def memberships_return(session_id: str = "", user: CurrentUser = Me, db: AsyncSession = Db):
    """Back from the payment provider. What happened is asked of the provider, not read from the URL."""
    if session_id:
        try:
            outcome = await membership_service.verify_payment(db, user, session_id)
            await db.commit()
            if outcome == "refund_due":
                await membership_service.refund_unhonoured(db, session_id)
                await db.commit()
        except AppError:
            await db.rollback()
    return RedirectResponse("/memberships", status_code=303)


# --- FairSeat (buying) ------------------------------------------------------------------


@router.get("/tickets", dependencies=needs("tickets"))
async def tickets(request: Request, user: CurrentUser = Me, db: AsyncSession = Db):
    membership = await membership_service.my_membership(db, user) if modules.has("memberships") else None
    bookings = [b for b in await fairseat_service.list_my_bookings(db, user) if b.status == "pending_payment"]
    return render(
        request, "tickets.html", user,
        events=await ticket_service.discover(db, user), holds=bookings, membership=membership,
        payments_on=get_settings().fairseat_payments_enabled,
        event_names=dict((await db.execute(select(Event.id, Event.name).where(Event.club_id == user.club_id))).all()),
    )


@router.get("/tickets/mine", dependencies=needs("tickets"))
async def my_tickets(request: Request, user: CurrentUser = Me, db: AsyncSession = Db):
    found = await gatesync_service.my_tickets(db, user)
    await db.commit()  # credentials issued on first view
    return render(request, "tickets_mine.html", user, bookings=found)


@router.get("/tickets/return", dependencies=needs("tickets"))
async def tickets_return(session_id: str = "", user: CurrentUser = Me, db: AsyncSession = Db):
    """Back from the payment provider after paying for (or abandoning) a booking."""
    if session_id and get_settings().fairseat_payments_enabled:
        try:
            booking_id, outcome = await fairseat_payments.verify_payment(db, user, session_id)
            await db.commit()
            if outcome == "refund_required":  # paid too late for the seats: send the money back
                await fairseat_payments.process_refund(db, booking_id)
                await db.commit()
        except AppError:
            await db.rollback()
            return RedirectResponse("/tickets", status_code=303)
        return RedirectResponse("/tickets/mine", status_code=303)
    return RedirectResponse("/tickets", status_code=303)


# --- GateSync ---------------------------------------------------------------------------


@router.get("/check-in", dependencies=needs("checkin"))
async def check_in(request: Request, user: CurrentUser = Me, db: AsyncSession = Db):
    return render(request, "checkin.html", user, events=await gatesync_service.events_for_door(db, user))


@router.get("/check-in/{event_id}", dependencies=needs("checkin"))
async def check_in_event(event_id: uuid.UUID, request: Request, user: CurrentUser = Me, db: AsyncSession = Db):
    return render(request, "checkin_event.html", user, door=await gatesync_service.attendance(db, user, event_id))


# --- CrewDoo Connect --------------------------------------------------------------------


@router.get("/announcements", dependencies=needs("announcements"))
async def announcements(request: Request, user: CurrentUser = Me, db: AsyncSession = Db):
    can_post = announcement_service.can_post(user)
    return render(
        request, "announcements.html", user, items=await announcement_service.feed(db, user),
        can_post=can_post, events=await _club_events(db, user) if can_post else [],
    )


# --- CertiDoo ---------------------------------------------------------------------------


@router.get("/certificates", dependencies=needs("certificates"))
async def certificates(request: Request, user: CurrentUser = Me, db: AsyncSession = Db):
    issuer = user.is_finance_admin or user.role == "event_lead"
    events = [e for e in await _club_events(db, user) if certificate_service.can_issue(user, e)] if issuer else []
    people = []
    if user.is_finance_admin:
        people = list(await db.scalars(select(User).where(User.club_id == user.club_id).order_by(User.full_name)))
    return render(
        request, "certificates.html", user, mine=await certificate_service.mine(db, user),
        issued=await certificate_service.issued_in_club(db, user) if issuer else [],
        issuer=issuer, events=events, people=people, attendance=modules.has("checkin"),
    )


@router.get("/verify")
async def verify_form(request: Request, code: str = "", db: AsyncSession = Db, _=Depends(modules.require("certificates"))):
    """Public: check a certificate by the code printed on it. No sign-in."""
    code = code.strip()
    found = await certificate_service.verify(db, code) if code else None
    return render(request, "certificate_verify.html", None, code=code, certificate=found, checked=bool(code))


@router.get("/verify/{code}")
async def verify_code(code: str, request: Request, db: AsyncSession = Db, _=Depends(modules.require("certificates"))):
    found = await certificate_service.verify(db, code)
    return render(request, "certificate_verify.html", None, code=code, certificate=found, checked=True)


@router.get("/certificates/{certificate_id}", dependencies=needs("certificates"))
async def certificate(certificate_id: uuid.UUID, request: Request, user: CurrentUser = Me, db: AsyncSession = Db):
    found = await certificate_service.get(db, user, certificate_id)
    link = f"{get_settings().app_base_url.rstrip('/')}/verify/{found.verify_code}"
    return render(request, "certificate.html", user, certificate=found, verify_url=link,
                  qr_svg=gatesync_service.qr_svg(link))


# --- LegacyVault ------------------------------------------------------------------------


@router.get("/handover", dependencies=needs("handover"))
async def handover(request: Request, archived: bool = False, user: CurrentUser = Me, db: AsyncSession = Db):
    return render(
        request, "handover.html", user, notes=await handover_service.list_notes(db, user, include_archived=archived),
        writable=handover_service.writable_roles(user), show_archived=archived,
        can_see_archived=user.role in ("event_lead", "treasurer", "president"),
    )
