import asyncio
import hashlib
import hmac
import json
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.models.ledger import LedgerEntry
from app.models.merch import ProductVariant
from app.services import payment_service

MERCH = "/api/v1/merch"
PAY = "/api/v1/payments"


@pytest.fixture
async def hoodie(client, world, auth):
    """Club hoodie: ₹999, ₹799 for members. M has 10 in stock; XXL costs ₹100 more and has 1."""
    response = await client.post(
        f"{MERCH}/products",
        headers=auth(world.treasurer),
        json={
            "name": "Club Hoodie", "description": "Heavyweight fleece", "base_price": "999", "member_price": "799",
            "variants": [
                {"size": "M", "color": "Black", "sku": "HOOD-BLK-M", "stock_quantity": 10},
                {"size": "XXL", "color": "Black", "sku": "HOOD-BLK-XXL", "stock_quantity": 1, "price_override": "1099"},
            ],
        },
    )
    assert response.status_code == 201, response.text
    product = response.json()
    by_sku = {variant["sku"]: variant for variant in product["variants"]}
    return {"product": product, "m": by_sku["HOOD-BLK-M"], "xxl": by_sku["HOOD-BLK-XXL"]}


async def _stock(db, variant) -> int:
    return await db.scalar(select(ProductVariant.stock_quantity).where(ProductVariant.id == variant["id"]))


async def _merch_entries(db) -> list[LedgerEntry]:
    rows = await db.scalars(
        select(LedgerEntry).where(LedgerEntry.source_module == "merchandise").order_by(LedgerEntry.created_at)
    )
    return list(rows)


@pytest.fixture
def checkout(client, auth):
    """Order one variant and open a payment order for it, stopping just before payment."""

    async def _checkout(user, variant, quantity=1):
        order = await client.post(
            f"{MERCH}/orders", headers=auth(user), json={"items": [{"variant_id": variant["id"], "quantity": quantity}]}
        )
        assert order.status_code == 201, order.text
        payment = await client.post(
            f"{PAY}/razorpay/create-order", headers=auth(user), json={"merch_order_id": order.json()["id"]}
        )
        assert payment.status_code == 200, payment.text
        return order.json(), payment.json()

    return _checkout


@pytest.fixture
def pay(client, auth):
    async def _pay(user, payment, outcome="success"):
        simulated = await client.post(
            f"{PAY}/mock/simulate", headers=auth(user),
            json={"razorpay_order_id": payment["razorpay_order_id"], "outcome": outcome},
        )
        assert simulated.status_code == 200, simulated.text
        return await client.post(f"{PAY}/razorpay/verify", headers=auth(user), json=simulated.json())

    return _pay


# --- Catalogue and pricing -------------------------------------------------------------


async def test_catalogue_is_managed_by_finance_admins(client, world, auth, hoodie):
    body = {"name": "Sticker", "base_price": "50", "member_price": "40"}
    assert (await client.post(f"{MERCH}/products", headers=auth(world.member), json=body)).status_code == 403
    assert (await client.post(f"{MERCH}/products", headers=auth(world.lead), json=body)).status_code == 403

    pricey = await client.post(
        f"{MERCH}/products", headers=auth(world.treasurer), json=body | {"member_price": "60"}
    )
    assert pricey.status_code == 422

    variants_url = f"{MERCH}/products/{hoodie['product']['id']}/variants"
    duplicate = await client.post(
        variants_url, headers=auth(world.treasurer), json={"size": "L", "color": "Black", "sku": "hood-blk-m"}
    )
    assert duplicate.status_code == 409 and duplicate.json()["error"]["code"] == "duplicate_sku"
    added = await client.post(
        variants_url, headers=auth(world.treasurer),
        json={"size": "L", "color": "Black", "sku": "HOOD-BLK-L", "stock_quantity": 4},
    )
    assert added.status_code == 201 and len(added.json()["variants"]) == 3

    restocked = await client.put(
        f"{MERCH}/variants/{hoodie['m']['id']}", headers=auth(world.treasurer), json={"stock_quantity": 25}
    )
    assert restocked.status_code == 200
    assert {v["sku"]: v["stock_quantity"] for v in restocked.json()["variants"]}["HOOD-BLK-M"] == 25
    negative = await client.put(
        f"{MERCH}/variants/{hoodie['m']['id']}", headers=auth(world.treasurer), json={"stock_quantity": -1}
    )
    assert negative.status_code == 422

    # Retired products disappear from the store but stay visible to admins.
    await client.put(
        f"{MERCH}/products/{hoodie['product']['id']}", headers=auth(world.treasurer), json={"is_active": False}
    )
    assert (await client.get(f"{MERCH}/products", headers=auth(world.member))).json() == []
    assert (await client.get(f"{MERCH}/products/{hoodie['product']['id']}", headers=auth(world.member))).status_code == 404
    assert (await client.get(f"{MERCH}/products?include_inactive=true", headers=auth(world.member))).status_code == 403
    admin_view = await client.get(f"{MERCH}/products?include_inactive=true", headers=auth(world.treasurer))
    assert len(admin_view.json()) == 1


async def test_price_is_decided_by_the_server(client, world, auth, hoodie):
    as_member = (await client.get(f"{MERCH}/products", headers=auth(world.member))).json()[0]
    as_outsider = (await client.get(f"{MERCH}/products", headers=auth(world.outsider))).json()[0]
    assert (as_member["your_price"], as_member["member_pricing_applied"]) == (799.0, True)
    assert (as_outsider["your_price"], as_outsider["member_pricing_applied"]) == (999.0, False)

    def price(product, sku):
        return next(v["your_price"] for v in product["variants"] if v["sku"] == sku)

    # The XXL override replaces the base price; the ₹200 member discount still applies.
    assert (price(as_member, "HOOD-BLK-XXL"), price(as_outsider, "HOOD-BLK-XXL")) == (899.0, 1099.0)

    # A non-member who sends the member price (or any price) still pays the real one.
    order = await client.post(
        f"{MERCH}/orders",
        headers=auth(world.outsider),
        json={"items": [{"variant_id": hoodie["m"]["id"], "quantity": 2, "unit_price": 1}], "total_amount": 1},
    )
    assert order.status_code == 201
    assert order.json()["total_amount"] == 1998.0
    assert order.json()["items"][0]["unit_price"] == 999.0


# --- Order, pay, ledger ------------------------------------------------------------------


async def test_member_buys_a_hoodie(client, db, world, auth, hoodie, checkout, pay):
    order, payment = await checkout(world.member, hoodie["m"])
    assert order["total_amount"] == 799.0
    assert (order["status"], order["payment_status"]) == ("created", "created")
    assert payment["is_mock"] is True and payment["provider"] == "mock"
    assert payment["amount"] == 79900  # paise, from the server's order

    # Nothing moves until the payment is verified.
    assert await _stock(db, hoodie["m"]) == 10
    assert await _merch_entries(db) == []

    verified = await pay(world.member, payment)
    assert verified.status_code == 200, verified.text
    paid = verified.json()["order"]
    assert verified.json()["verified"] is True
    assert (paid["status"], paid["payment_status"]) == ("paid", "paid")
    assert paid["razorpay_payment_id"].startswith("pay_mock_")

    assert await _stock(db, hoodie["m"]) == 9
    [entry] = await _merch_entries(db)
    assert (entry.entry_type, entry.direction, entry.payment_status) == ("income", "credit", "paid")
    assert (entry.amount, str(entry.reference_id), entry.reference_type) == (Decimal("799.00"), order["id"], "merch_order")
    assert entry.payer_id == world.member.id

    summary = (await client.get("/api/v1/ledger/summary", headers=auth(world.treasurer))).json()
    assert summary["income_by_source"]["merchandise"] == 799.0
    assert summary["current_balance"] == 799.0


async def test_verifying_twice_and_webhook_retries_change_nothing(client, db, world, auth, hoodie, checkout):
    order, payment = await checkout(world.member, hoodie["m"])
    simulated = (await client.post(
        f"{PAY}/mock/simulate", headers=auth(world.member), json={"razorpay_order_id": payment["razorpay_order_id"]}
    )).json()

    for _ in range(3):
        response = await client.post(f"{PAY}/razorpay/verify", headers=auth(world.member), json=simulated)
        assert response.status_code == 200

    webhook = json.dumps({"event": "payment.captured", "payload": {"payment": {"entity": {
        "id": simulated["razorpay_payment_id"], "order_id": payment["razorpay_order_id"], "amount": 79900}}}}).encode()
    signature = payment_service.get_provider().sign_webhook(webhook)
    for _ in range(2):
        ack = await client.post(f"{PAY}/razorpay/webhook", content=webhook, headers={"X-Razorpay-Signature": signature})
        assert ack.status_code == 200 and ack.json()["status"] == "processed"

    assert await _stock(db, hoodie["m"]) == 9
    assert len(await _merch_entries(db)) == 1


async def test_unverified_payment_is_rejected(client, db, world, auth, hoodie, checkout, pay):
    order, payment = await checkout(world.member, hoodie["m"])

    failed = await pay(world.member, payment, outcome="failure")
    assert failed.status_code == 400
    assert failed.json()["error"]["code"] == "payment_verification_failed"

    after = (await client.get(f"{MERCH}/orders/{order['id']}", headers=auth(world.member))).json()
    assert (after["status"], after["payment_status"]) == ("cancelled", "failed")
    assert await _stock(db, hoodie["m"]) == 10
    assert await _merch_entries(db) == []

    # A closed order cannot be paid afterwards.
    reopen = await client.post(
        f"{PAY}/razorpay/create-order", headers=auth(world.member), json={"merch_order_id": order["id"]}
    )
    assert reopen.status_code == 409


async def test_forged_signatures_do_not_pass(client, db, world, auth, hoodie, checkout):
    order, payment = await checkout(world.member, hoodie["m"])
    payment_id = "pay_forged_0001"
    forged = hmac.new(b"guessed-secret", f"{payment['razorpay_order_id']}|{payment_id}".encode(), hashlib.sha256).hexdigest()
    response = await client.post(f"{PAY}/razorpay/verify", headers=auth(world.member), json={
        "razorpay_order_id": payment["razorpay_order_id"], "razorpay_payment_id": payment_id, "razorpay_signature": forged,
    })
    assert response.status_code == 400
    assert await _stock(db, hoodie["m"]) == 10 and await _merch_entries(db) == []


def test_signature_matches_razorpay_algorithm():
    secret = get_settings().mock_payment_secret
    provider = payment_service.get_provider()
    expected = hmac.new(secret.encode(), b"order_abc|pay_xyz", hashlib.sha256).hexdigest()
    assert payment_service.payment_signature(secret, "order_abc", "pay_xyz") == expected
    assert provider.verify_payment_signature("order_abc", "pay_xyz", expected) is True
    assert provider.verify_payment_signature("order_abc", "pay_other", expected) is False
    assert provider.verify_payment_signature("order_other", "pay_xyz", expected) is False
    assert provider.verify_payment_signature("order_abc", "pay_xyz", "") is False


async def test_orders_are_private(client, world, auth, hoodie, checkout):
    order, payment = await checkout(world.member, hoodie["m"])
    assert (await client.get(f"{MERCH}/orders/{order['id']}", headers=auth(world.outsider))).status_code == 404
    assert (await client.get(f"{MERCH}/orders", headers=auth(world.outsider))).json() == []
    assert (await client.get(f"{MERCH}/orders?all=true", headers=auth(world.outsider))).status_code == 403
    assert len((await client.get(f"{MERCH}/orders?all=true", headers=auth(world.treasurer))).json()) == 1

    # Nobody else can pay for, or simulate a payment on, my order.
    steal = await client.post(
        f"{PAY}/razorpay/create-order", headers=auth(world.outsider), json={"merch_order_id": order["id"]}
    )
    assert steal.status_code == 404
    simulate = await client.post(
        f"{PAY}/mock/simulate", headers=auth(world.outsider), json={"razorpay_order_id": payment["razorpay_order_id"]}
    )
    assert simulate.status_code == 404


# --- Stock ---------------------------------------------------------------------------------


async def test_cannot_order_more_than_is_in_stock(client, world, auth, hoodie):
    response = await client.post(
        f"{MERCH}/orders", headers=auth(world.member), json={"items": [{"variant_id": hoodie["xxl"]["id"], "quantity": 2}]}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "insufficient_stock"
    assert response.json()["error"]["details"]["available"] == 1


async def test_last_item_cannot_be_sold_twice(client, db, world, auth, hoodie, checkout, pay):
    # Both buyers get as far as paying for the single XXL.
    _, first_payment = await checkout(world.member, hoodie["xxl"])
    second_order, second_payment = await checkout(world.outsider, hoodie["xxl"])

    assert (await pay(world.member, first_payment)).status_code == 200
    late = await pay(world.outsider, second_payment)
    assert late.status_code == 409
    assert late.json()["error"]["code"] == "out_of_stock"

    after = (await client.get(f"{MERCH}/orders/{second_order['id']}", headers=auth(world.outsider))).json()
    assert (after["status"], after["payment_status"]) == ("cancelled", "refunded")
    assert await _stock(db, hoodie["xxl"]) == 0
    assert [entry.amount for entry in await _merch_entries(db)] == [Decimal("899.00")]

    # And once it is gone, a new order is refused up front.
    sold_out = await client.post(
        f"{MERCH}/orders", headers=auth(world.volunteer), json={"items": [{"variant_id": hoodie["xxl"]["id"]}]}
    )
    assert sold_out.status_code == 409


async def test_simultaneous_payments_for_the_last_item(client, db, world, auth, hoodie, checkout):
    buyers = [world.member, world.outsider, world.volunteer, world.lead]
    payments = [(buyer, (await checkout(buyer, hoodie["xxl"]))[1]) for buyer in buyers]

    async def pay_now(buyer, payment):
        simulated = await client.post(
            f"{PAY}/mock/simulate", headers=auth(buyer), json={"razorpay_order_id": payment["razorpay_order_id"]}
        )
        return await client.post(f"{PAY}/razorpay/verify", headers=auth(buyer), json=simulated.json())

    results = await asyncio.gather(*(pay_now(buyer, payment) for buyer, payment in payments))
    assert sorted(response.status_code for response in results) == [200, 409, 409, 409]
    assert await _stock(db, hoodie["xxl"]) == 0
    assert len(await _merch_entries(db)) == 1


# --- Webhook ---------------------------------------------------------------------------------


def _webhook(event: str, payment_id: str, order_id: str, amount: int) -> tuple[bytes, str]:
    body = json.dumps({"event": event, "payload": {"payment": {"entity": {
        "id": payment_id, "order_id": order_id, "amount": amount}}}}).encode()
    return body, payment_service.get_provider().sign_webhook(body)


async def test_webhook_fulfils_an_order(client, db, world, hoodie, checkout):
    order, payment = await checkout(world.member, hoodie["m"])
    body, signature = _webhook("payment.captured", "pay_hook_001", payment["razorpay_order_id"], 79900)

    unsigned = await client.post(f"{PAY}/razorpay/webhook", content=body)
    assert unsigned.status_code == 401
    wrong = await client.post(f"{PAY}/razorpay/webhook", content=body, headers={"X-Razorpay-Signature": "f" * 64})
    assert wrong.status_code == 401 and wrong.json()["error"]["code"] == "invalid_webhook_signature"
    assert await _stock(db, hoodie["m"]) == 10

    ack = await client.post(f"{PAY}/razorpay/webhook", content=body, headers={"X-Razorpay-Signature": signature})
    assert ack.status_code == 200 and ack.json() == {"status": "processed", "detail": "Order paid"}
    assert await _stock(db, hoodie["m"]) == 9
    assert len(await _merch_entries(db)) == 1


async def test_webhook_ignores_wrong_amounts_and_handles_failures(client, db, world, auth, hoodie, checkout):
    order, payment = await checkout(world.member, hoodie["m"])

    body, signature = _webhook("payment.captured", "pay_hook_cheap", payment["razorpay_order_id"], 100)
    ack = await client.post(f"{PAY}/razorpay/webhook", content=body, headers={"X-Razorpay-Signature": signature})
    assert ack.json()["status"] == "ignored"
    assert await _stock(db, hoodie["m"]) == 10 and await _merch_entries(db) == []

    body, signature = _webhook("payment.failed", "pay_hook_fail", payment["razorpay_order_id"], 79900)
    ack = await client.post(f"{PAY}/razorpay/webhook", content=body, headers={"X-Razorpay-Signature": signature})
    assert ack.json()["status"] == "processed"
    after = (await client.get(f"{MERCH}/orders/{order['id']}", headers=auth(world.member))).json()
    assert (after["status"], after["payment_status"]) == ("cancelled", "failed")
    assert await _stock(db, hoodie["m"]) == 10


# --- Fulfilment and refunds --------------------------------------------------------------------


async def test_cancelling_a_paid_order_restocks_and_refunds(client, db, world, auth, hoodie, checkout, pay):
    order, payment = await checkout(world.member, hoodie["m"], quantity=2)
    await pay(world.member, payment)
    assert await _stock(db, hoodie["m"]) == 8
    status_url = f"{MERCH}/orders/{order['id']}/status"

    assert (await client.post(status_url, headers=auth(world.member), json={"status": "cancelled"})).status_code == 403
    skip = await client.post(status_url, headers=auth(world.treasurer), json={"status": "delivered"})
    assert skip.status_code == 409 and skip.json()["error"]["code"] == "invalid_status_change"

    packed = await client.post(status_url, headers=auth(world.treasurer), json={"status": "packed"})
    assert packed.json()["status"] == "packed"

    cancelled = await client.post(status_url, headers=auth(world.treasurer), json={"status": "cancelled"})
    assert cancelled.status_code == 200
    assert (cancelled.json()["status"], cancelled.json()["payment_status"]) == ("cancelled", "refunded")
    assert await _stock(db, hoodie["m"]) == 10

    income, refund = await _merch_entries(db)
    assert (income.entry_type, income.payment_status, income.amount) == ("income", "paid", Decimal("1598.00"))
    assert (refund.entry_type, refund.direction, refund.amount) == ("refund", "debit", Decimal("1598.00"))
    assert refund.reference_id == income.reference_id

    summary = (await client.get("/api/v1/ledger/summary", headers=auth(world.treasurer))).json()
    assert (summary["current_balance"], summary["total_refunds"]) == (0.0, 1598.0)

    again = await client.post(status_url, headers=auth(world.treasurer), json={"status": "cancelled"})
    assert again.status_code == 409
    assert await db.scalar(select(func.count()).select_from(LedgerEntry)) == 2


async def test_delivered_orders_cannot_be_cancelled(client, world, auth, hoodie, checkout, pay):
    order, payment = await checkout(world.member, hoodie["m"])
    await pay(world.member, payment)
    status_url = f"{MERCH}/orders/{order['id']}/status"
    for step in ("packed", "delivered"):
        assert (await client.post(status_url, headers=auth(world.treasurer), json={"status": step})).status_code == 200
    assert (await client.post(status_url, headers=auth(world.treasurer), json={"status": "cancelled"})).status_code == 409
