"""Which modules this database can use.

MemberCore, FairSeat, GateSync, CrewDoo Connect, CertiDoo, LegacyVault and Member 2's additions
each have their own tables, created by `make migrate` like everything else. A module is offered
when its tables are there, so a database that has not been migrated yet still runs: the pages
answer 404 and the menu lists the module as not here yet. Tickets also honour FAIRSEAT_ENABLED,
which is on unless set to 0.
"""

import time

from fastapi import Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.dependencies import get_db
from app.core.errors import AppError

# module -> (label in the menu, a table that only exists once its migration has run)
MODULES = {
    "memberships": ("Memberships", "membership_checkouts"),
    "tickets": ("Tickets", "event_tickets"),
    "checkin": ("Check-in", "event_registrations"),
    "announcements": ("Announcements", "announcements"),
    "certificates": ("Certificates", "certificates"),
    "handover": ("Handover", "handover_note_versions"),
    # Member 2's Batch A and B (migrations/040-043). These extend existing pages rather
    # than being destinations of their own, so the menu never lists them (UNLISTED).
    "profile": ("Profile & UPI", "user_payment_profiles"),
    "reminders": ("Renewal reminders", "membership_renewal_reminders"),
    "budgets": ("Event budgets", "event_budgets"),
    "crew_shifts": ("Volunteer shifts", "crew_task_shifts"),
}
UNLISTED = ("profile", "reminders", "budgets", "crew_shifts")
NEEDS_FAIRSEAT = ("tickets",)

_tables: frozenset[str] | None = None
_looked_at: float = 0.0
# While a module is missing its table is looked for again this often, so a database migrated
# while the app is running is noticed without a restart. Once everything is there, never again.
RECHECK_SECONDS = 15.0


async def load(db: AsyncSession = Depends(get_db)) -> frozenset[str]:
    """Modules that can be used. One query the first time; none once every module is present."""
    global _tables, _looked_at
    complete = _tables is not None and len(_tables) == len(MODULES)
    if _tables is None or (not complete and time.monotonic() - _looked_at >= RECHECK_SECONDS):
        rows = await db.execute(
            text("SELECT name FROM unnest(CAST(:names AS text[])) AS name WHERE to_regclass(name) IS NOT NULL"),
            {"names": [table for _, table in MODULES.values()]},
        )
        present = set(rows.scalars())
        _tables = frozenset(name for name, (_, table) in MODULES.items() if table in present)
        _looked_at = time.monotonic()
    return available()


def available() -> frozenset[str]:
    """What `load` found, without touching the database. Empty until `load` has run."""
    found = _tables or frozenset()
    if not get_settings().fairseat_enabled:
        found = found - set(NEEDS_FAIRSEAT)
    return found


def migrated(name: str) -> bool:
    """The module's tables exist, whether or not it is switched on."""
    return name in (_tables or frozenset())


def has(name: str) -> bool:
    return name in available()


def forget() -> None:
    """Check the tables again on the next request (after migrating; used by tests)."""
    global _tables, _looked_at
    _tables, _looked_at = None, 0.0


def require(name: str):
    """A route dependency: 404 unless the module is usable here."""

    async def check(found: frozenset[str] = Depends(load)) -> None:
        if name not in found:
            raise AppError(404, "module_unavailable", f"{MODULES[name][0]} is not switched on in this deployment")

    return check
