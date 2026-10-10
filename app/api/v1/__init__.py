from fastapi import APIRouter

from app.api.v1 import events, expenses, fairseat, ledger, merch, payments, tasks

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
