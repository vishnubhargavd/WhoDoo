from fastapi import APIRouter

from app.api.v1 import expenses, ledger

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(ledger.router)
api_router.include_router(expenses.router)
