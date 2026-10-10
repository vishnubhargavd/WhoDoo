"""Which optional modules this deployment can use.

MemberCore, GateSync, CrewDoo Connect, CertiDoo and LegacyVault keep their tables in
migrations/pending/, which `make migrate` does not apply. A module is offered only when its
tables are there (`make migrate-pending` on a local database); otherwise its pages and
routes answer 404 and the menu keeps showing it as not here yet. Tickets also need
FAIRSEAT_ENABLED. Check-in comes with public registration (migrations/012), which is always
applied, and admits FairSeat tickets too once those are switched on.
"""

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
}
NEEDS_FAIRSEAT = ("tickets",)

_tables: frozenset[str] | None = None


async def load(db: AsyncSession = Depends(get_db)) -> frozenset[str]:
    """Modules that can be used. The table check runs once per process."""
    global _tables
    if _tables is None:
        rows = await db.execute(
            text("SELECT name FROM unnest(CAST(:names AS text[])) AS name WHERE to_regclass(name) IS NOT NULL"),
            {"names": [table for _, table in MODULES.values()]},
        )
        present = set(rows.scalars())
        _tables = frozenset(name for name, (_, table) in MODULES.items() if table in present)
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
    global _tables
    _tables = None


def require(name: str):
    """A route dependency: 404 unless the module is usable here."""

    async def check(found: frozenset[str] = Depends(load)) -> None:
        if name not in found:
            raise AppError(404, "module_unavailable", f"{MODULES[name][0]} is not switched on in this deployment")

    return check
