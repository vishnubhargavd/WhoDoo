"""Apply SQL migrations to the database in DATABASE_URL.

    python -m scripts.migrate            # apply pending migrations
    python -m scripts.migrate --reset    # drop everything first (local development only)
"""

import argparse
import asyncio

from app.core.config import get_settings
from app.db.database import asyncpg_dsn
from app.db.migrate import run_migrations


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="drop the public schema before migrating")
    args = parser.parse_args()

    dsn = asyncpg_dsn(get_settings().database_url)
    applied = asyncio.run(run_migrations(dsn, reset=args.reset))
    print("Applied:", ", ".join(applied) if applied else "nothing (already up to date)")


if __name__ == "__main__":
    main()
