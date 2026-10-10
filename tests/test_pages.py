"""Smoke tests: every review page renders for the right role."""

from decimal import Decimal

from app.core.config import get_settings
from app.core.security import create_access_token
from app.services import ledger_service


def _cookie(user) -> dict[str, str]:
    return {get_settings().session_cookie: create_access_token(user.id)}


async def test_pages_redirect_to_sign_in(client):
    response = await client.get("/treasurer")
    assert response.status_code == 303
    assert response.headers["location"] == "/dev/login?next=/treasurer"
    assert (await client.get("/dev/login")).status_code == 200


async def test_treasurer_pages(client, db, world):
    await ledger_service.record_income(
        db, club_id=world.club.id, source_module="event_ticket", amount=Decimal("123456.5"),
        created_by=world.treasurer.id, event_id=world.bake_sale.id, description="Ticket batch",
    )
    await db.commit()
    client.cookies.update(_cookie(world.treasurer))

    dashboard = await client.get("/treasurer")
    assert dashboard.status_code == 200
    assert "₹1,23,456.50" in dashboard.text
    assert "Bake Sale" in dashboard.text

    ledger = await client.get("/treasurer/ledger", params={"entry_type": "income", "date_from": "", "q": "ticket"})
    assert ledger.status_code == 200
    assert "Ticket batch" in ledger.text

    bad_filter = await client.get("/treasurer/ledger", params={"entry_type": "nonsense"})
    assert bad_filter.status_code == 200 and "Filters ignored" in bad_filter.text


async def test_treasurer_pages_forbidden_for_volunteers(client, world):
    client.cookies.update(_cookie(world.volunteer))
    response = await client.get("/treasurer")
    assert response.status_code == 403
    assert "permission" in response.text or "roles" in response.text
