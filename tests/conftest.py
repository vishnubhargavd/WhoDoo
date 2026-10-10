"""Test setup. Runs against a real local Postgres (default: the `whodoo_test` database),
because the rules under test live partly in the database: the immutability trigger,
unique keys and the conditional stock update."""

import os
import tempfile
import uuid
from datetime import date
from types import SimpleNamespace

os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://localhost/whodoo_test")
os.environ["DB_NULL_POOL"] = "1"
os.environ["DEMO_AUTH"] = "1"
os.environ["JWT_SECRET"] = "test-secret-that-is-long-enough-for-hs256"
os.environ["PAYMENT_PROVIDER"] = "mock"
os.environ["EXPENSE_AUTO_APPROVAL_LIMIT"] = "1000"
os.environ["SUPABASE_URL"] = ""
os.environ["FAIRSEAT_ENABLED"] = "1"
os.environ["UPLOAD_DIR"] = tempfile.mkdtemp(prefix="whodoo-test-uploads-")

import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.security import create_access_token  # noqa: E402
from app.db.database import SessionLocal, asyncpg_dsn  # noqa: E402
from app.db.migrate import run_migrations  # noqa: E402
from app.main import app  # noqa: E402
from app.models.shared import Club, Event, User  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
async def _schema():
    url = get_settings().database_url
    assert "test" in url.rsplit("/", 1)[-1], f"Refusing to reset a non-test database: {url}"
    await run_migrations(asyncpg_dsn(url), reset=True)


@pytest.fixture(scope="session")
async def fairseat_schema(_schema):
    """FairSeat's tables are still a pending migration: `make migrate` does not apply it.
    Tests add it to the disposable test database, on top of the real migrations."""
    import asyncpg

    from app.db.migrate import MIGRATIONS_DIR

    url = get_settings().database_url
    assert "test" in url.rsplit("/", 1)[-1] and get_settings().database_is_local, f"Not a local test database: {url}"
    conn = await asyncpg.connect(asyncpg_dsn(url))
    try:
        if await conn.fetchval("SELECT to_regclass('public.ticket_categories')") is None:
            async with conn.transaction():
                await conn.execute((MIGRATIONS_DIR / "pending" / "020_fairseat.sql").read_text())
    finally:
        await conn.close()


@pytest.fixture
async def db(_schema):
    async with SessionLocal() as session:
        tables = await session.scalars(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'schema_migrations'")
        )
        await session.execute(text("TRUNCATE " + ", ".join(tables) + " CASCADE"))
        await session.commit()
        yield session


@pytest.fixture
async def world(db):
    """One club with a person in every role, and two events led by different leads."""
    club = Club(id=uuid.uuid4(), name="Test Club")
    db.add(club)
    await db.flush()

    def person(name: str, role: str, member: bool = True) -> User:
        user = User(
            id=uuid.uuid4(),
            club_id=club.id,
            full_name=name,
            email=f"{name.lower().replace(' ', '.')}@test.example",
            role=role,
            is_active_member=member,
        )
        db.add(user)
        return user

    people = SimpleNamespace(
        president=person("Priya President", "president"),
        treasurer=person("Tara Treasurer", "treasurer"),
        lead=person("Leo Lead", "event_lead"),
        lead2=person("Lina Lead", "event_lead"),
        volunteer=person("Vik Volunteer", "volunteer"),
        member=person("Mia Member", "member"),
        outsider=person("Omar Outsider", "member", member=False),
    )
    await db.flush()
    bake_sale = Event(id=uuid.uuid4(), club_id=club.id, name="Bake Sale", event_date=date(2026, 10, 3), lead_id=people.lead.id)
    fest = Event(id=uuid.uuid4(), club_id=club.id, name="Tech Fest", event_date=date(2026, 9, 12), lead_id=people.lead2.id)
    db.add_all([bake_sale, fest])
    await db.commit()
    return SimpleNamespace(club=club, bake_sale=bake_sale, fest=fest, **vars(people))


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.fixture
def auth():
    def headers(user) -> dict[str, str]:
        return {"Authorization": f"Bearer {create_access_token(user.id)}"}

    return headers
