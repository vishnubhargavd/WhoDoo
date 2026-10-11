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


async def test_expense_pages(client, world, auth):
    from tests.test_expenses import API, RECEIPT

    upload = await client.post(f"{API}/ocr", headers=auth(world.volunteer), files={"file": ("r.png", RECEIPT, "image/png")})
    created = await client.post(API, headers=auth(world.volunteer), json={
        "receipt_id": upload.json()["receipt_id"], "title": "Stage decor", "description": "Backdrop and lights",
        "category": "decoration", "amount": "2500", "expense_date": "2026-09-28", "event_id": str(world.fest.id),
    })
    claim_id = created.json()["id"]

    client.cookies.update(_cookie(world.volunteer))
    assert (await client.get("/expenses/new")).status_code == 200
    mine = await client.get("/expenses")
    assert mine.status_code == 200 and "Stage decor" in mine.text and "Lina Lead" in mine.text
    detail = await client.get(f"/expenses/{claim_id}")
    assert detail.status_code == 200 and "₹2,500.00" in detail.text
    assert "data-post" not in detail.text  # the submitter gets no approval buttons
    assert (await client.get("/expenses/approvals")).status_code == 403

    client.cookies.update(_cookie(world.lead2))
    queue = await client.get("/expenses/approvals")
    assert queue.status_code == 200 and "First of two" in queue.text
    assert f"/api/v1/expenses/{claim_id}/approve" in queue.text

    await client.post(f"{API}/{claim_id}/request-changes", headers=auth(world.lead2), json={"comment": "Add the invoice"})
    client.cookies.update(_cookie(world.volunteer))
    assert "Resubmit for approval" in (await client.get(f"/expenses/{claim_id}")).text


async def test_store_and_merch_admin_pages(client, world, auth):
    created = await client.post("/api/v1/merch/products", headers=auth(world.treasurer), json={
        "name": "Club Hoodie", "base_price": "999", "member_price": "799",
        "variants": [{"size": "M", "color": "Black", "sku": "HOOD-BLK-M", "stock_quantity": 10}],
    })
    product = created.json()
    order = (await client.post("/api/v1/merch/orders", headers=auth(world.member), json={
        "items": [{"variant_id": product["variants"][0]["id"], "quantity": 1}]})).json()

    client.cookies.update(_cookie(world.member))
    store = await client.get("/store")
    assert store.status_code == 200 and "Club Hoodie" in store.text and "₹799.00" in store.text
    detail = await client.get(f"/store/{product['id']}")
    assert detail.status_code == 200 and "10 left" in detail.text
    checkout = await client.get(f"/checkout/{order['id']}")
    assert checkout.status_code == 200
    assert "Mock Payment (Stripe-compatible)" in checkout.text and "(mock)" in checkout.text and 'class="pay-bar"' in checkout.text
    mine = await client.get("/orders")
    assert mine.status_code == 200 and order["order_number"] in mine.text and "Pay now" in mine.text
    assert (await client.get("/admin/merch")).status_code == 403
    assert (await client.get("/admin/merch/orders")).status_code == 403

    # Someone else's checkout page does not exist for them.
    client.cookies.update(_cookie(world.outsider))
    assert (await client.get(f"/checkout/{order['id']}")).status_code == 404
    assert "₹999.00" in (await client.get("/store")).text

    client.cookies.update(_cookie(world.treasurer))
    admin = await client.get("/admin/merch")
    assert admin.status_code == 200 and "HOOD-BLK-M" in admin.text
    admin_orders = await client.get("/admin/merch/orders")
    assert admin_orders.status_code == 200 and "Mia Member" in admin_orders.text


async def test_claim_page_shows_receipt_items_total_and_progress(client, world, auth):
    import shutil

    import pytest

    if shutil.which("tesseract") is None:
        pytest.skip("Tesseract is not installed")
    from tests.test_receipt_ocr import API, MULTI_ITEM

    upload = (await client.post(f"{API}/ocr", headers=auth(world.volunteer), files={"file": ("b.png", MULTI_ITEM, "image/png")})).json()
    claim = (await client.post(API, headers=auth(world.volunteer), json={
        "receipt_id": upload["receipt_id"], "title": "Nature Basket Stores purchase", "description": "Stall supplies",
        "category": "supplies", "amount": "968.00", "expense_date": "2026-09-23", "event_id": str(world.fest.id),
    })).json()

    client.cookies.update(_cookie(world.volunteer))
    page = (await client.get(f"/expenses/{claim['id']}")).text
    assert "Nature Basket Stores purchase" in page                     # claim title
    assert "Nature Basket Stores</td>" in page                          # merchant read from the receipt
    assert "Orange Powder" in page and "₹448.00" in page                # first item and its price
    assert "Cheese" in page and "₹112.00" in page                       # last item
    assert "Total on receipt" in page and "₹968.00" in page             # the total, distinct from any item
    assert "Matches receipt" in page
    assert "Awaiting approval from Lina Lead" in page

    # ₹968 is under the limit, so the lead's approval completes it and the member sees that at once.
    await client.post(f"{API}/{claim['id']}/approve", headers=auth(world.lead2))
    assert "Approved · awaiting payment" in (await client.get("/expenses")).text

    # A large claim: after the lead approves, the member sees the first approval, not a bare "pending".
    upload = (await client.post(f"{API}/ocr", headers=auth(world.volunteer), files={"file": ("b.png", MULTI_ITEM, "image/png")})).json()
    large = (await client.post(API, headers=auth(world.volunteer), json={
        "receipt_id": upload["receipt_id"], "title": "Projector hire", "description": "Two days",
        "category": "other", "amount": "1800.00", "expense_date": "2026-09-23", "event_id": str(world.fest.id),
    })).json()
    await client.post(f"{API}/{large['id']}/approve", headers=auth(world.lead2))
    for path in ("/expenses", f"/expenses/{large['id']}"):
        text = (await client.get(path)).text
        assert "First approval done" in text
        assert "First approval by Lina Lead · awaiting final approval from Tara Treasurer" in text
    assert "Edited by submitter" in (await client.get(f"/expenses/{large['id']}")).text


async def test_pages_are_branded_whodoo(client, world):
    client.cookies.update(_cookie(world.treasurer))
    for path in ("/treasurer", "/expenses", "/store"):
        page = (await client.get(path)).text
        assert "<title>" in page and "WhoDoo" in page and "WhoDoo Finance" not in page
