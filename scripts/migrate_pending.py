"""Apply migrations/pending/*.sql to the LOCAL database in DATABASE_URL.

    python -m scripts.migrate_pending

These are the tables for FairSeat, MemberCore, GateSync, CrewDoo Connect, CertiDoo and
LegacyVault. They are kept out of `scripts.migrate` on purpose: nothing here may reach a
shared or hosted database until the team approves it, so this refuses any database that is
not on this machine. Run `python -m scripts.migrate` first.
"""

import asyncio

from app.core.config import get_settings
from app.db.database import asyncpg_dsn
from app.db.migrate import run_pending_migrations


def main() -> None:
    settings = get_settings()
    if not settings.database_is_local:
        raise SystemExit("Refusing: DATABASE_URL is not a local database. Pending migrations are local-only.")
    applied = asyncio.run(run_pending_migrations(asyncpg_dsn(settings.database_url)))
    print("Applied:", ", ".join(applied) if applied else "nothing (already up to date)")
    if not settings.fairseat_enabled:
        print("Note: set FAIRSEAT_ENABLED=1 and FAIRSEAT_PAYMENTS_ENABLED=1 in .env to use Tickets and Check-in.")


if __name__ == "__main__":
    main()
