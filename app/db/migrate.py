"""Tiny SQL migration runner: applies migrations/*.sql in name order, once each."""

from pathlib import Path

import asyncpg

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


async def run_migrations(dsn: str, *, reset: bool = False) -> list[str]:
    """Apply pending migrations and return their names. `reset` drops the public schema first."""
    conn = await asyncpg.connect(dsn)
    try:
        if reset:
            db_name = await conn.fetchval("SELECT current_database()")
            if "test" not in db_name:
                raise RuntimeError(f"Refusing to reset non-test database: {db_name}")
            await conn.execute("DROP SCHEMA IF EXISTS membercore_internal CASCADE; DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
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
