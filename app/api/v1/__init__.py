from fastapi import APIRouter

from app.api.v1 import (
    community,
    event_tickets,
    events,
    expenses,
    fairseat,
    fairseat_payments,
    ledger,
    member2,
    memberships,
    merch,
    payments,
    tasks,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(ledger.router)
api_router.include_router(expenses.router)
api_router.include_router(merch.router)
api_router.include_router(payments.router)
# Member 2: Event Management, CrewPulse, Event Autopilot, SyncSphere
api_router.include_router(events.router)
api_router.include_router(tasks.router)
# Member 1: FairSeat ticket booking (answers 404 unless FAIRSEAT_ENABLED=1)
api_router.include_router(fairseat.router)
# Member 2: FairSeat event side (ticket settings, event discovery), same switch
api_router.include_router(event_tickets.router)
api_router.include_router(event_tickets.discovery)
api_router.include_router(fairseat_payments.router)
# MemberCore, GateSync, CrewDoo Connect, CertiDoo, LegacyVault (each 404 until its tables exist)
api_router.include_router(memberships.router)
api_router.include_router(community.checkin)
api_router.include_router(community.announcements)
api_router.include_router(community.public)
api_router.include_router(community.certificates)
api_router.include_router(community.handover)
# Member 2, Batch A: profile and UPI, renewal reminders, payouts, registrations and budget, Razorpay
api_router.include_router(member2.me)
api_router.include_router(member2.payouts)
api_router.include_router(member2.events)
api_router.include_router(member2.razorpay)
