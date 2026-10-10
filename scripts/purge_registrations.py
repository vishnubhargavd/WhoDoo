"""Delete participants of events that ended more than REGISTRATION_RETENTION_DAYS ago.

    python -m scripts.purge_registrations

The app does this itself about once a day; this is for running it on demand or from cron.
Names, phone numbers, emails, student IDs and QR records are deleted. Ledger entries stay.
"""

import asyncio

from app.core.config import get_settings
from app.db.database import SessionLocal
from app.services import registration_service


async def main() -> None:
    async with SessionLocal() as session:
        removed = await registration_service.purge_expired(session)
        await session.commit()
    print(f"Deleted {removed} registrations older than {get_settings().registration_retention_days} days after their event.")


if __name__ == "__main__":
    asyncio.run(main())
