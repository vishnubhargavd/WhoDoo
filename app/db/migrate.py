"""Tiny SQL migration runner: applies migrations/*.sql in name order, once each."""

from pathlib import Path

import asyncpg

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


async def run_migrations(dsn: str, *, reset: bool = False) -> list[str]:
    """Apply pending migrations and return their names. `reset` drops the public schema first."""
    conn = await asyncpg.connect(dsn)
    try:
        if reset:
            # MemberCore (pending/031) keeps its trigger functions in a schema of its own.
            await conn.execute(
                "DROP SCHEMA public CASCADE; DROP SCHEMA IF EXISTS membercore_internal CASCADE; CREATE SCHEMA public;"
            )
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {row["name"] for row in await conn.fetch("SELECT name FROM schema_migrations")}
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
    """Apply migrations/pending/*.sql that have not run here yet, recorded as `pending/<name>`.

    These are the optional modules' tables. `run_migrations` never applies them; the caller
    decides where they may go (scripts/migrate_pending.py allows a local database only).
    """
    conn = await asyncpg.connect(dsn)
    try:
        done = {row["name"] for row in await conn.fetch("SELECT name FROM schema_migrations")}
        applied = []
        for path in sorted((MIGRATIONS_DIR / "pending").glob("*.sql")):
            name = f"pending/{path.name}"
            if name in done:
                continue
            async with conn.transaction():
                await conn.execute(path.read_text())
                await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)
            applied.append(name)
        return applied
    finally:
        await conn.close()
