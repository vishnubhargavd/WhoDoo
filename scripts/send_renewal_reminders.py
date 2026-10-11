"""Write due membership renewal reminders for every club. Safe to run as often as you like.

    python -m scripts.send_renewal_reminders

The app already does this on a timer (RENEWAL_REMINDER_INTERVAL_SECONDS) and whenever a member
opens Home, Memberships or Notifications; this is for running it from cron instead.
"""

import asyncio

from app.db.database import SessionLocal
from app.services import reminder_service


async def main() -> None:
    async with SessionLocal() as db:
        written = await reminder_service.run_all(db)
        await db.commit()
    print(f"Renewal reminders written: {written}")


if __name__ == "__main__":
    asyncio.run(main())
