"""Tiny SQL migration runner: applies migrations/*.sql in name order, once each."""

from pathlib import Path

import asyncpg

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


async def run_migrations(dsn: str, *, reset: bool = False) -> list[str]:
    """Apply pending migrations and return their names. `reset` drops the public schema first."""
    conn = await asyncpg.connect(dsn)
    try:
        if reset:
            # MemberCore (migration 031) keeps its trigger functions in a schema of its own.
            await conn.execute(
                "DROP SCHEMA public CASCADE; DROP SCHEMA IF EXISTS membercore_internal CASCADE; CREATE SCHEMA public;"
            )
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        # A database that took the modules while they were optional recorded them as
        # "pending/<name>". Those count as applied, so they are not run a second time.
        done = {row["name"].removeprefix("pending/") for row in await conn.fetch("SELECT name FROM schema_migrations")}
        applied = []
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            async with conn.transaction():
                await conn.execute(path.read_text())
                await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", path.name)
            applied.append(path.name)
        return applied
    finally:
        await conn.close()


async def run_pending_migrations(dsn: str) -> list[str]:
    """Kept for scripts that still call it. The optional modules' migrations used to live in
    migrations/; they are ordinary migrations now, so this is the same as run_migrations."""
    return await run_migrations(dsn)
