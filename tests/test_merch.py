import asyncio
import hashlib
import hmac
import json
import time
from decimal import Decimal

import pytest
import stripe
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.errors import AppError
from app.models.ledger import LedgerEntry
from app.models.merch import PaymentEvent, ProductVariant
from app.services import payment_service

MERCH = "/api/v1/merch"
PAY = "/api/v1/payments"
STRIPE_KEY = "sk_test_dummy_key_for_tests"
WEBHOOK_SECRET = "whsec_test_secret"


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


async def _balance(client, auth, world) -> float:
    return (await client.get("/api/v1/ledger/summary", headers=auth(world.treasurer))).json()["current_balance"]


@pytest.fixture
def checkout(client, auth):
    """Order one variant and open a checkout session for it, stopping just before payment."""

    async def _checkout(user, variant, quantity=1):
        order = await client.post(
            f"{MERCH}/orders", headers=auth(user), json={"items": [{"variant_id": variant["id"], "quantity": quantity}]}
        )
        assert order.status_code == 201, order.text
        payment = await client.post(
            f"{PAY}/stripe/create-checkout-session", headers=auth(user), json={"merch_order_id": order.json()["id"]}
        )
        assert payment.status_code == 200, payment.text
        return order.json(), payment.json()

    return _checkout


@pytest.fixture
def pay(client, auth):
    """Mock provider: pay (or decline) on the checkout page, then come back and verify."""

    async def _pay(user, payment, outcome="success"):
        simulated = await client.post(
            f"{PAY}/mock/simulate", headers=auth(user), json={"session_id": payment["session_id"], "outcome": outcome}
        )
        assert simulated.status_code == 200, simulated.text
        return await client.post(f"{PAY}/stripe/verify", headers=auth(user), json={"session_id": payment["session_id"]})

    return _pay


def _event(event_id: str, kind: str, obj: dict, secret: str | None = None) -> tuple[bytes, dict[str, str]]:
    """A webhook body and its Stripe-Signature header, signed the way Stripe signs."""
    body = json.dumps({"id": event_id, "object": "event", "type": kind, "data": {"object": obj}}).encode()
    stamp = int(time.time())
    secret = secret or get_settings().mock_webhook_secret
    digest = hmac.new(secret.encode(), f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()
    return body, {"Stripe-Signature": f"t={stamp},v1={digest}"}


def _completed(order: dict, payment: dict, intent: str, amount: int | None = None) -> dict:
    return {"id": payment["session_id"], "object": "checkout.session", "payment_status": "paid",
            "amount_total": payment["amount"] if amount is None else amount, "currency": "inr",
            "payment_intent": intent, "metadata": {"merch_order_id": order["id"]}}


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


async def test_order_creation_does_not_touch_stock(client, db, world, auth, hoodie, checkout):
    order, payment = await checkout(world.member, hoodie["m"], quantity=3)
    assert (order["status"], order["payment_status"], order["currency"]) == ("created", "pending", "INR")
    assert payment["session_id"].startswith("cs_mock_")
    assert await _stock(db, hoodie["m"]) == 10
    assert await _merch_entries(db) == []
    assert await _balance(client, auth, world) == 0.0


async def test_member_buys_a_hoodie(client, db, world, auth, hoodie, checkout, pay):
    order, payment = await checkout(world.member, hoodie["m"])
    assert order["total_amount"] == 799.0
    assert payment["is_mock"] is True and payment["provider"] == "mock"
    assert payment["amount"] == 79900  # paise, from the server's order

    # Opening checkout again reuses the open session instead of creating another.
    again = await client.post(
        f"{PAY}/stripe/create-checkout-session", headers=auth(world.member), json={"merch_order_id": order["id"]}
    )
    assert again.json()["session_id"] == payment["session_id"]

    # Nothing moves until the payment is verified, and an unpaid session does not verify.
    early = await client.post(f"{PAY}/stripe/verify", headers=auth(world.member), json={"session_id": payment["session_id"]})
    assert early.status_code == 409 and early.json()["error"]["code"] == "payment_not_completed"
    assert await _stock(db, hoodie["m"]) == 10
    assert await _merch_entries(db) == []

    verified = await pay(world.member, payment)
    assert verified.status_code == 200, verified.text
    paid = verified.json()["order"]
    assert verified.json()["verified"] is True
    assert (paid["status"], paid["payment_status"]) == ("paid", "paid")
    assert paid["stripe_checkout_session_id"] == payment["session_id"]
    assert paid["stripe_payment_intent_id"].startswith("pi_mock_")

    assert await _stock(db, hoodie["m"]) == 9
    [entry] = await _merch_entries(db)
    assert (entry.entry_type, entry.direction, entry.payment_status) == ("income", "credit", "paid")
    assert (entry.amount, str(entry.reference_id), entry.reference_type) == (Decimal("799.00"), order["id"], "merch_order")
    assert (entry.category, entry.currency, entry.event_id) == ("merchandise_sales", "INR", None)
    assert entry.description.startswith("Merchandise purchase: 1 x Club Hoodie")
    assert entry.payer_id == world.member.id
    assert entry.idempotency_key == f"{paid['stripe_payment_intent_id']}:income"

    summary = (await client.get("/api/v1/ledger/summary", headers=auth(world.treasurer))).json()
    assert summary["income_by_source"]["merchandise"] == 799.0
    assert (summary["total_income"], summary["current_balance"]) == (799.0, 799.0)


async def test_verifying_twice_and_webhook_retries_change_nothing(client, db, world, auth, hoodie, checkout, pay):
    order, payment = await checkout(world.member, hoodie["m"])
    for _ in range(3):
        response = await pay(world.member, payment)
        assert response.status_code == 200
    intent = response.json()["order"]["stripe_payment_intent_id"]

    # Stripe sends both of these for one payment, and may send each more than once.
    body, headers = _event("evt_session_1", "checkout.session.completed", _completed(order, payment, intent))
    first = await client.post(f"{PAY}/stripe/webhook", content=body, headers=headers)
    assert first.status_code == 200 and first.json() == {"status": "processed", "detail": "Order paid"}
    repeat = await client.post(f"{PAY}/stripe/webhook", content=body, headers=headers)
    assert repeat.status_code == 200 and repeat.json()["status"] == "duplicate"

    body, headers = _event("evt_intent_1", "payment_intent.succeeded", {
        "id": intent, "object": "payment_intent", "amount_received": 79900, "currency": "inr",
        "metadata": {"merch_order_id": order["id"]}})
    for expected in ("processed", "duplicate"):
        ack = await client.post(f"{PAY}/stripe/webhook", content=body, headers=headers)
        assert ack.status_code == 200 and ack.json()["status"] == expected

    assert await _stock(db, hoodie["m"]) == 9
    assert len(await _merch_entries(db)) == 1
    assert await _balance(client, auth, world) == 799.0
    assert await db.scalar(select(func.count()).select_from(PaymentEvent)) == 2


async def test_declined_payment_changes_nothing_and_can_be_retried(client, db, world, auth, hoodie, checkout, pay):
    order, payment = await checkout(world.member, hoodie["m"])

    failed = await pay(world.member, payment, outcome="failure")
    assert failed.status_code == 409
    assert failed.json()["error"]["code"] == "payment_not_completed"

    after = (await client.get(f"{MERCH}/orders/{order['id']}", headers=auth(world.member))).json()
    assert (after["status"], after["payment_status"]) == ("created", "failed")
    assert await _stock(db, hoodie["m"]) == 10
    assert await _merch_entries(db) == []

    # Like Stripe's checkout page, the buyer can try another card on the same order.
    assert (await pay(world.member, payment)).status_code == 200
    assert await _stock(db, hoodie["m"]) == 9 and len(await _merch_entries(db)) == 1


async def test_made_up_sessions_do_not_verify(client, db, world, auth, hoodie, checkout):
    order, payment = await checkout(world.member, hoodie["m"])
    unknown = await client.post(f"{PAY}/stripe/verify", headers=auth(world.member), json={"session_id": "cs_test_made_up"})
    assert unknown.status_code == 404
    # The client cannot assert an amount or a status: extra fields are simply not read.
    forged = await client.post(f"{PAY}/stripe/verify", headers=auth(world.member), json={
        "session_id": payment["session_id"], "payment_status": "paid", "amount_total": 79900})
    assert forged.status_code == 409
    assert await _stock(db, hoodie["m"]) == 10 and await _merch_entries(db) == []


async def test_orders_are_private(client, world, auth, hoodie, checkout):
    order, payment = await checkout(world.member, hoodie["m"])
    assert (await client.get(f"{MERCH}/orders/{order['id']}", headers=auth(world.outsider))).status_code == 404
    assert (await client.get(f"{MERCH}/orders", headers=auth(world.outsider))).json() == []
    assert (await client.get(f"{MERCH}/orders?all=true", headers=auth(world.outsider))).status_code == 403
    assert len((await client.get(f"{MERCH}/orders?all=true", headers=auth(world.treasurer))).json()) == 1

    # Nobody else can open checkout for, pay, or verify my order.
    steal = await client.post(
        f"{PAY}/stripe/create-checkout-session", headers=auth(world.outsider), json={"merch_order_id": order["id"]}
    )
    assert steal.status_code == 404
    for path in ("mock/simulate", "stripe/verify"):
        other = await client.post(f"{PAY}/{path}", headers=auth(world.outsider), json={"session_id": payment["session_id"]})
        assert other.status_code == 404
    assert (await client.post(f"{PAY}/stripe/verify", json={"session_id": payment["session_id"]})).status_code == 401


# --- Stock ---------------------------------------------------------------------------------


async def test_cannot_order_more_than_is_in_stock(client, world, auth, hoodie):
    response = await client.post(
        f"{MERCH}/orders", headers=auth(world.member), json={"items": [{"variant_id": hoodie["xxl"]["id"], "quantity": 2}]}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "insufficient_stock"
    assert response.json()["error"]["details"]["available"] == 1


async def test_paid_but_sold_out_is_refunded_in_full(client, db, world, auth, hoodie, checkout, pay):
    # Both buyers get as far as paying for the single XXL.
    _, first_payment = await checkout(world.member, hoodie["xxl"])
    second_order, second_payment = await checkout(world.outsider, hoodie["xxl"])

    assert (await pay(world.member, first_payment)).status_code == 200
    late = await pay(world.outsider, second_payment)
    assert late.status_code == 409
    assert late.json()["error"]["code"] == "insufficient_stock"

    after = (await client.get(f"{MERCH}/orders/{second_order['id']}", headers=auth(world.outsider))).json()
    assert (after["status"], after["payment_status"]) == ("cancelled", "refunded")
    intent = after["stripe_payment_intent_id"]
    assert await _stock(db, hoodie["xxl"]) == 0

    # The ledger shows the second buyer's money arriving and going straight back.
    sale, charged, returned = await _merch_entries(db)
    assert (sale.entry_type, sale.amount) == ("income", Decimal("899.00"))
    assert (charged.entry_type, charged.amount, charged.idempotency_key) == ("income", Decimal("1099.00"), f"{intent}:income")
    assert (returned.entry_type, returned.direction, returned.amount) == ("refund", "debit", Decimal("1099.00"))
    assert returned.idempotency_key == f"{intent}:refund"
    assert await _balance(client, auth, world) == 899.0
    assert payment_service.get_provider().refunds[f"{intent}:refund"] == (intent, 109900)

    # Coming back to the page again neither refunds nor posts anything a second time.
    refunds_so_far = len(payment_service.get_provider().refunds)
    again = await client.post(
        f"{PAY}/stripe/verify", headers=auth(world.outsider), json={"session_id": second_payment["session_id"]})
    assert again.status_code == 409 and again.json()["error"]["code"] == "insufficient_stock"
    assert len(await _merch_entries(db)) == 3 and len(payment_service.get_provider().refunds) == refunds_so_far

    # And once it is gone, a new order is refused up front.
    sold_out = await client.post(
        f"{MERCH}/orders", headers=auth(world.volunteer), json={"items": [{"variant_id": hoodie["xxl"]["id"]}]}
    )
    assert sold_out.status_code == 409 and sold_out.json()["error"]["code"] == "insufficient_stock"


async def test_simultaneous_payments_for_the_last_item(client, db, world, auth, hoodie, checkout, pay):
    buyers = [world.member, world.outsider, world.volunteer, world.lead]
    payments = [(buyer, (await checkout(buyer, hoodie["xxl"]))[1]) for buyer in buyers]

    results = await asyncio.gather(*(pay(buyer, payment) for buyer, payment in payments))
    assert sorted(response.status_code for response in results) == [200, 409, 409, 409]
    assert await _stock(db, hoodie["xxl"]) == 0
    kinds = [entry.entry_type for entry in await _merch_entries(db)]
    assert (kinds.count("income"), kinds.count("refund")) == (4, 3)  # one sale kept, three charged and refunded
    assert await _balance(client, auth, world) in (899.0, 1099.0)


# --- Webhook ---------------------------------------------------------------------------------


async def test_webhook_fulfils_an_order_when_the_buyer_never_returns(client, db, world, auth, hoodie, checkout):
    order, payment = await checkout(world.member, hoodie["m"])
    body, headers = _event("evt_hook_1", "checkout.session.completed", _completed(order, payment, "pi_hook_001"))

    ack = await client.post(f"{PAY}/stripe/webhook", content=body, headers=headers)
    assert ack.status_code == 200 and ack.json() == {"status": "processed", "detail": "Order paid"}
    assert await _stock(db, hoodie["m"]) == 9
    [entry] = await _merch_entries(db)
    assert entry.idempotency_key == "pi_hook_001:income"
    paid = (await client.get(f"{MERCH}/orders/{order['id']}", headers=auth(world.member))).json()
    assert (paid["status"], paid["payment_status"], paid["stripe_payment_intent_id"]) == ("paid", "paid", "pi_hook_001")

    logged = await db.scalar(select(PaymentEvent).where(PaymentEvent.provider_event_id == "evt_hook_1"))
    assert (logged.event_type, logged.outcome, str(logged.merch_order_id)) == (
        "checkout.session.completed", "processed", order["id"])


async def test_webhook_signature_is_checked_by_stripes_own_verifier(client, db, world, hoodie, checkout):
    order, payment = await checkout(world.member, hoodie["m"])
    body, headers = _event("evt_sig_1", "checkout.session.completed", _completed(order, payment, "pi_sig_001"))

    unsigned = await client.post(f"{PAY}/stripe/webhook", content=body)
    assert unsigned.status_code == 400 and unsigned.json()["error"]["code"] == "invalid_webhook_signature"

    _, wrong_secret = _event("evt_sig_1", "checkout.session.completed", _completed(order, payment, "pi_sig_001"),
                             secret="whsec_guessed")
    tampered = body.replace(b"79900", b"1")
    stale_stamp = int(time.time()) - 3600
    stale = hmac.new(get_settings().mock_webhook_secret.encode(), f"{stale_stamp}.".encode() + body, hashlib.sha256)
    attempts = [
        (body, wrong_secret),
        (tampered, headers),
        (body, {"Stripe-Signature": "t=1,v1=" + "f" * 64}),
        (body, {"Stripe-Signature": "not a signature"}),
        (body, {"Stripe-Signature": f"t={stale_stamp},v1={stale.hexdigest()}"}),  # replay of an old delivery
    ]
    for content, bad in attempts:
        response = await client.post(f"{PAY}/stripe/webhook", content=content, headers=bad)
        assert response.status_code == 400, bad
    assert await _stock(db, hoodie["m"]) == 10 and await _merch_entries(db) == []
    assert await db.scalar(select(func.count()).select_from(PaymentEvent)) == 0

    # The header our helper builds is accepted by Stripe's verifier, and rejected with another secret.
    secret = get_settings().mock_webhook_secret
    assert stripe.Webhook.construct_event(body, headers["Stripe-Signature"], secret)["id"] == "evt_sig_1"
    with pytest.raises(stripe.SignatureVerificationError):
        stripe.Webhook.construct_event(body, headers["Stripe-Signature"], "whsec_other")
    with pytest.raises(payment_service.InvalidWebhookSignature):
        payment_service.parse_webhook(body, headers["Stripe-Signature"], "")  # an unset secret verifies nothing


async def test_webhook_ignores_wrong_amounts_and_notes_failures(client, db, world, auth, hoodie, checkout):
    order, payment = await checkout(world.member, hoodie["m"])

    body, headers = _event("evt_cheap", "checkout.session.completed", _completed(order, payment, "pi_cheap", amount=100))
    ack = await client.post(f"{PAY}/stripe/webhook", content=body, headers=headers)
    assert ack.json() == {"status": "ignored", "detail": "Amount does not match the order"}
    assert await _stock(db, hoodie["m"]) == 10 and await _merch_entries(db) == []

    body, headers = _event("evt_failed", "payment_intent.payment_failed", {
        "id": "pi_declined", "object": "payment_intent", "metadata": {"merch_order_id": order["id"]}})
    ack = await client.post(f"{PAY}/stripe/webhook", content=body, headers=headers)
    assert ack.json()["status"] == "processed"
    after = (await client.get(f"{MERCH}/orders/{order['id']}", headers=auth(world.member))).json()
    assert (after["status"], after["payment_status"]) == ("created", "failed")
    assert await _stock(db, hoodie["m"]) == 10

    body, headers = _event("evt_other", "customer.created", {"id": "cus_1", "object": "customer"})
    assert (await client.post(f"{PAY}/stripe/webhook", content=body, headers=headers)).json()["status"] == "ignored"


async def test_refund_made_in_the_stripe_dashboard_is_recorded_once(client, db, world, auth, hoodie, checkout, pay):
    order, payment = await checkout(world.member, hoodie["m"])
    intent = (await pay(world.member, payment)).json()["order"]["stripe_payment_intent_id"]
    assert await _stock(db, hoodie["m"]) == 9

    charge = {"id": "ch_1", "object": "charge", "payment_intent": intent, "amount": 79900, "amount_refunded": 79900}
    for event_id, expected in (("evt_refund_1", "Order refunded"), ("evt_refund_2", "Already refunded")):
        body, headers = _event(event_id, "charge.refunded", charge)
        ack = await client.post(f"{PAY}/stripe/webhook", content=body, headers=headers)
        assert ack.json() == {"status": "processed", "detail": expected}

    after = (await client.get(f"{MERCH}/orders/{order['id']}", headers=auth(world.member))).json()
    assert (after["status"], after["payment_status"]) == ("cancelled", "refunded")
    assert await _stock(db, hoodie["m"]) == 10
    income, refund = await _merch_entries(db)
    assert (refund.entry_type, refund.amount, refund.idempotency_key) == ("refund", Decimal("799.00"), f"{intent}:refund")
    assert await _balance(client, auth, world) == 0.0


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

    # The money goes back through the provider, once, for the full amount.
    assert list(payment_service.get_provider().refunds.values())[-1] == (income.idempotency_key.split(":")[0], 159800)
    assert refund.idempotency_key == income.idempotency_key.replace(":income", ":refund")

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


# --- Stripe provider (network calls stubbed) ---------------------------------------------------


class FakeStripe:
    """Stands in for Stripe's servers. Everything else, including the SDK's objects, is real."""

    def __init__(self):
        self.sessions: dict[str, dict] = {}
        self.created: list[dict] = []
        self.refunds: list[dict] = []
        self.reject_key = False

    def create_session(self, **params):
        if self.reject_key:
            raise stripe.AuthenticationError("Invalid API Key provided")
        self.created.append(params)
        number = len(self.created)
        total = sum(line["price_data"]["unit_amount"] * line["quantity"] for line in params["line_items"])
        self.sessions[f"cs_test_{number}"] = {
            "id": f"cs_test_{number}", "object": "checkout.session", "url": f"https://checkout.stripe.com/c/pay/cs_test_{number}",
            "status": "open", "payment_status": "unpaid", "amount_total": total,
            "currency": params["line_items"][0]["price_data"]["currency"], "payment_intent": None,
            "metadata": params["metadata"],
        }
        return stripe.checkout.Session.construct_from(self.sessions[f"cs_test_{number}"], params["api_key"])

    def retrieve_session(self, session_id, **params):
        if session_id not in self.sessions:
            raise stripe.InvalidRequestError(f"No such checkout.session: {session_id}", "id")
        return stripe.checkout.Session.construct_from(self.sessions[session_id], params["api_key"])

    def create_refund(self, **params):
        self.refunds.append(params)
        return stripe.Refund.construct_from({"id": f"re_{len(self.refunds)}", "status": "succeeded"}, params["api_key"])

    def pay(self, session_id: str) -> str:
        """The buyer completes Stripe's hosted checkout with a test card."""
        intent = f"pi_test_{session_id[-1]}"
        self.sessions[session_id].update(status="complete", payment_status="paid", payment_intent=intent)
        return intent


@pytest.fixture
def stripe_mode(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "payment_provider", "stripe")
    monkeypatch.setattr(settings, "stripe_secret_key", STRIPE_KEY)
    monkeypatch.setattr(settings, "stripe_webhook_secret", WEBHOOK_SECRET)
    fake = FakeStripe()
    monkeypatch.setattr(stripe.checkout.Session, "create", fake.create_session)
    monkeypatch.setattr(stripe.checkout.Session, "retrieve", fake.retrieve_session)
    monkeypatch.setattr(stripe.Refund, "create", fake.create_refund)
    payment_service.get_provider.cache_clear()
    yield fake
    monkeypatch.undo()
    payment_service.get_provider.cache_clear()


async def test_stripe_checkout_end_to_end(client, db, world, auth, hoodie, checkout, stripe_mode):
    order, payment = await checkout(world.member, hoodie["m"])
    assert (payment["provider"], payment["is_mock"]) == ("stripe", False)
    assert payment["checkout_url"] == "https://checkout.stripe.com/c/pay/cs_test_1"
    assert STRIPE_KEY not in json.dumps(payment)  # the secret key never reaches the browser

    [sent] = stripe_mode.created
    assert (sent["mode"], sent["api_key"]) == ("payment", STRIPE_KEY)
    [line] = sent["line_items"]
    assert (line["quantity"], line["price_data"]["currency"], line["price_data"]["unit_amount"]) == (1, "inr", 79900)
    assert sent["metadata"]["merch_order_id"] == order["id"] == sent["payment_intent_data"]["metadata"]["merch_order_id"]
    assert (sent["metadata"]["user_id"], sent["metadata"]["club_id"]) == (str(world.member.id), str(world.club.id))
    assert (sent["metadata"]["product_variant_id"], sent["metadata"]["quantity"]) == (hoodie["m"]["id"], "1")
    assert sent["success_url"].endswith(f"/orders/{order['id']}?session_id={{CHECKOUT_SESSION_ID}}")
    assert sent["cancel_url"].endswith(f"/checkout/{order['id']}?cancelled=1")

    # Mock payments are not available while Stripe is the provider.
    simulate = await client.post(f"{PAY}/mock/simulate", headers=auth(world.member), json={"session_id": "cs_test_1"})
    assert simulate.status_code == 404

    verify = {"headers": auth(world.member), "json": {"session_id": "cs_test_1"}}
    assert (await client.post(f"{PAY}/stripe/verify", **verify)).status_code == 409  # not paid yet
    assert await _stock(db, hoodie["m"]) == 10

    intent = stripe_mode.pay("cs_test_1")
    for _ in range(3):  # the success redirect, then two page refreshes
        verified = await client.post(f"{PAY}/stripe/verify", **verify)
        assert verified.status_code == 200, verified.text
    paid = verified.json()["order"]
    assert (paid["status"], paid["payment_status"], paid["stripe_payment_intent_id"]) == ("paid", "paid", intent)
    assert await _stock(db, hoodie["m"]) == 9
    [entry] = await _merch_entries(db)
    assert (entry.amount, entry.category, entry.idempotency_key) == (Decimal("799.00"), "merchandise_sales", f"{intent}:income")
    assert await _balance(client, auth, world) == 799.0
    assert stripe_mode.refunds == []


async def test_stripe_amount_mismatch_is_rejected(client, db, world, auth, hoodie, checkout, stripe_mode):
    order, payment = await checkout(world.member, hoodie["m"])
    stripe_mode.pay("cs_test_1")
    verify = {"headers": auth(world.member), "json": {"session_id": "cs_test_1"}}

    stripe_mode.sessions["cs_test_1"]["amount_total"] = 100  # paid, but not what the order costs
    short = await client.post(f"{PAY}/stripe/verify", **verify)
    assert short.status_code == 400 and short.json()["error"]["code"] == "amount_mismatch"

    stripe_mode.sessions["cs_test_1"].update(amount_total=79900, currency="usd")
    assert (await client.post(f"{PAY}/stripe/verify", **verify)).json()["error"]["code"] == "amount_mismatch"

    # A paid session that was opened for a different order does not pay for this one.
    stripe_mode.sessions["cs_test_1"].update(currency="inr", metadata={"merch_order_id": str(world.club.id)})
    other = await client.post(f"{PAY}/stripe/verify", **verify)
    assert other.status_code == 400 and other.json()["error"]["code"] == "payment_verification_failed"

    after = (await client.get(f"{MERCH}/orders/{order['id']}", headers=auth(world.member))).json()
    assert (after["status"], after["payment_status"]) == ("created", "pending")
    assert await _stock(db, hoodie["m"]) == 10 and await _merch_entries(db) == []


async def test_stripe_webhook_uses_the_configured_secret(client, db, world, auth, hoodie, checkout, stripe_mode):
    order, payment = await checkout(world.member, hoodie["m"])
    intent = stripe_mode.pay("cs_test_1")
    session = _completed(order, payment, intent)

    body, mock_signed = _event("evt_live_1", "checkout.session.completed", session)  # signed with the mock secret
    assert (await client.post(f"{PAY}/stripe/webhook", content=body, headers=mock_signed)).status_code == 400
    assert (await client.post(f"{PAY}/stripe/webhook", content=body)).status_code == 400
    assert await _stock(db, hoodie["m"]) == 10

    body, headers = _event("evt_live_1", "checkout.session.completed", session, secret=WEBHOOK_SECRET)
    for expected in ("processed", "duplicate"):
        ack = await client.post(f"{PAY}/stripe/webhook", content=body, headers=headers)
        assert ack.status_code == 200 and ack.json()["status"] == expected
    # The buyer's browser arrives after the webhook: still one sale.
    late = await client.post(f"{PAY}/stripe/verify", headers=auth(world.member), json={"session_id": "cs_test_1"})
    assert late.status_code == 200
    assert await _stock(db, hoodie["m"]) == 9 and len(await _merch_entries(db)) == 1


async def test_stripe_checkout_completes_without_a_webhook_secret(client, db, world, auth, hoodie, checkout, stripe_mode, monkeypatch):
    monkeypatch.setattr(get_settings(), "stripe_webhook_secret", "")
    payment_service.get_provider.cache_clear()
    order, payment = await checkout(world.member, hoodie["m"])
    intent = stripe_mode.pay("cs_test_1")

    # With no secret, no webhook can be trusted, however it is signed.
    body, headers = _event("evt_nosecret", "checkout.session.completed", _completed(order, payment, intent), secret="")
    assert (await client.post(f"{PAY}/stripe/webhook", content=body, headers=headers)).status_code == 400

    # The success redirect alone still completes the purchase.
    verified = await client.post(f"{PAY}/stripe/verify", headers=auth(world.member), json={"session_id": "cs_test_1"})
    assert verified.status_code == 200 and verified.json()["order"]["payment_status"] == "paid"
    assert await _stock(db, hoodie["m"]) == 9 and len(await _merch_entries(db)) == 1


async def test_stripe_refunds_the_payment_intent_when_stock_ran_out(client, db, world, auth, hoodie, checkout, stripe_mode):
    _, _ = await checkout(world.member, hoodie["xxl"])
    late_order, _ = await checkout(world.outsider, hoodie["xxl"])
    stripe_mode.pay("cs_test_1")
    late_intent = stripe_mode.pay("cs_test_2")

    assert (await client.post(f"{PAY}/stripe/verify", headers=auth(world.member), json={"session_id": "cs_test_1"})).status_code == 200
    for _ in range(2):
        late = await client.post(f"{PAY}/stripe/verify", headers=auth(world.outsider), json={"session_id": "cs_test_2"})
        assert late.status_code == 409 and late.json()["error"]["code"] == "insufficient_stock"

    [refund] = stripe_mode.refunds  # one full refund, against the PaymentIntent
    assert (refund["payment_intent"], refund["amount"], refund["idempotency_key"]) == (
        late_intent, 109900, f"{late_intent}:refund")
    after = (await client.get(f"{MERCH}/orders/{late_order['id']}", headers=auth(world.outsider))).json()
    assert (after["status"], after["payment_status"]) == ("cancelled", "refunded")
    assert await _stock(db, hoodie["xxl"]) == 0
    assert await _balance(client, auth, world) == 899.0


async def test_rejected_stripe_key_fails_closed(client, db, world, auth, hoodie, stripe_mode):
    stripe_mode.reject_key = True
    order = await client.post(f"{MERCH}/orders", headers=auth(world.member),
                              json={"items": [{"variant_id": hoodie["m"]["id"]}]})
    result = await client.post(f"{PAY}/stripe/create-checkout-session", headers=auth(world.member),
                               json={"merch_order_id": order.json()["id"]})
    assert result.status_code == 503
    assert result.json()["error"]["code"] == "payments_unavailable"
    assert get_settings().payment_provider == "stripe"
    assert payment_service.get_provider().is_mock is False
    assert await _stock(db, hoodie["m"]) == 10 and await _merch_entries(db) == []


def test_only_test_mode_keys_select_stripe(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "payment_provider", "stripe")
    for key, expected in [("sk_test_abc", True), ("rk_test_abc", True), ("", False), ("sk_live_abc", False),
                          ("sk_test_REPLACE_WITH_NEW_SECRET_KEY", False), ("pk_test_abc", False)]:
        monkeypatch.setattr(settings, "stripe_secret_key", key)
        payment_service.get_provider.cache_clear()
        assert settings.use_stripe is expected, key
        if expected:
            assert payment_service.get_provider().is_mock is False
        else:
            with pytest.raises(AppError, match="configured"):
                payment_service.get_provider()
    monkeypatch.setattr(settings, "payment_provider", "mock")
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_abc")
    payment_service.get_provider.cache_clear()
    assert payment_service.get_provider().is_mock is True
    monkeypatch.undo()
    payment_service.get_provider.cache_clear()


@pytest.mark.parametrize("state", ["pending", "failed"])
async def test_merch_refunds_require_provider_settlement(client, db, world, auth, hoodie, checkout, pay, monkeypatch, state):
    order, payment = await checkout(world.member, hoodie["m"])
    await pay(world.member, payment)
    provider = payment_service.get_provider()
    monkeypatch.setattr(provider, "next_refund_status", state)
    response = await client.post(f"{MERCH}/orders/{order['id']}/status", headers=auth(world.treasurer),
                                 json={"status": "cancelled"})
    body = response.json()
    assert response.status_code == 200, body
    assert (body["status"], body["payment_status"], body["refund_status"]) == ("cancelled", "paid", state)
    assert await _stock(db, hoodie["m"]) == 10
    assert [e.entry_type for e in await _merch_entries(db)] == ["income"]
    assert await _balance(client, auth, world) == 799.0
    intent = body["stripe_payment_intent_id"]
    if state == "pending":
        refund = provider.refund_results[f"{intent}:refund"]
        for event_id in ("evt_merch_refund_settled", "evt_merch_refund_settled_again"):
            payload, headers = _event(event_id, "refund.updated", {
                "id": refund.id, "payment_intent": intent, "status": "succeeded", "amount": 79900, "currency": "inr"})
            assert (await client.post(f"{PAY}/stripe/webhook", content=payload, headers=headers)).status_code == 200
    else:
        monkeypatch.setattr(provider, "next_refund_status", "succeeded")
        denied = await client.post(f"{MERCH}/orders/{order['id']}/refund/retry", headers=auth(world.member))
        assert denied.status_code == 403
        retried = await client.post(f"{MERCH}/orders/{order['id']}/refund/retry", headers=auth(world.treasurer))
        assert retried.status_code == 200 and retried.json()["refund_status"] == "succeeded"
    assert await _stock(db, hoodie["m"]) == 10  # no second restock
    assert [e.entry_type for e in await _merch_entries(db)] == ["income", "refund"]
    assert await _balance(client, auth, world) == 0.0


async def test_merch_ambiguous_refund_reuses_key_and_releases_locks(client, db, world, auth, hoodie, checkout, pay, monkeypatch):
    import uuid
    from app.db.database import SessionLocal
    from app.models.merch import MerchOrder

    order, payment = await checkout(world.member, hoodie["m"])
    await pay(world.member, payment)
    provider = payment_service.get_provider()
    refund = provider.refund
    keys = []
    def interrupted(intent, paise, key):
        keys.append(key)
        result = refund(intent, paise, key)
        if len(keys) == 1:
            raise TimeoutError("response lost after provider accepted refund")
        return result
    monkeypatch.setattr(provider, "refund", interrupted)
    run = payment_service.run_in_threadpool
    async def check_unlocked(func, *args):
        if func is interrupted:
            async with SessionLocal() as probe:
                await probe.scalar(select(MerchOrder).where(MerchOrder.id == uuid.UUID(order["id"]))
                                   .with_for_update(nowait=True))
        return await run(func, *args)
    monkeypatch.setattr(payment_service, "run_in_threadpool", check_unlocked)
    cancelled = await client.post(f"{MERCH}/orders/{order['id']}/status", headers=auth(world.treasurer),
                                  json={"status": "cancelled"})
    assert (cancelled.json()["payment_status"], cancelled.json()["refund_status"]) == ("paid", "pending")
    assert [e.entry_type for e in await _merch_entries(db)] == ["income"]
    retried = await client.post(f"{MERCH}/orders/{order['id']}/refund/retry", headers=auth(world.treasurer))
    assert retried.json()["payment_status"] == "refunded"
    assert len(keys) == 2 and keys[0] == keys[1]
    assert [e.entry_type for e in await _merch_entries(db)] == ["income", "refund"]


async def test_sold_out_order_keeps_refund_pending_without_phantom_accounting(client, db, world, auth, hoodie, checkout, pay, monkeypatch):
    _, first = await checkout(world.member, hoodie["xxl"])
    late, second = await checkout(world.outsider, hoodie["xxl"])
    await pay(world.member, first)
    monkeypatch.setattr(payment_service.get_provider(), "next_refund_status", "pending")
    result = await pay(world.outsider, second)
    assert result.status_code == 409
    assert "refunded in full" not in result.text
    saved = (await client.get(f"{MERCH}/orders/{late['id']}", headers=auth(world.outsider))).json()
    assert (saved["status"], saved["payment_status"], saved["refund_status"]) == ("cancelled", "paid", "pending")
    assert await _stock(db, hoodie["xxl"]) == 0
    assert [e.entry_type for e in await _merch_entries(db)] == ["income", "income"]


@pytest.mark.parametrize("provider,demo,url", [
    ("auto", True, "postgresql://localhost/whodoo_test"),
    ("mock", False, "postgresql://localhost/whodoo_test"),
    ("mock", True, "postgresql://remote.invalid/whodoo_test"),
])
def test_mock_requires_explicit_local_demo(monkeypatch, provider, demo, url):
    settings = get_settings()
    monkeypatch.setattr(settings, "payment_provider", provider)
    monkeypatch.setattr(settings, "demo_auth", demo)
    monkeypatch.setattr(settings, "database_url", url)
    monkeypatch.setattr(settings, "stripe_secret_key", "")
    payment_service.get_provider.cache_clear()
    try:
        with pytest.raises(AppError) as caught:
            payment_service.get_provider()
        assert caught.value.status_code == 503
    finally:
        payment_service.get_provider.cache_clear()


# --- Several items in one order: titles, line prices and the total -----------------------------


async def test_multi_item_order_totals_agree_everywhere(client, db, world, auth, hoodie, pay):
    """Each line keeps its own price, and one total is used by the API, the payment and the ledger."""
    mug = (await client.post(f"{MERCH}/products", headers=auth(world.treasurer), json={
        "name": "Enamel Mug", "base_price": "299.50", "member_price": "249.50",
        "variants": [{"size": "Standard", "color": "White", "sku": "MUG-WHT", "stock_quantity": 5}],
    })).json()

    order = await client.post(f"{MERCH}/orders", headers=auth(world.member), json={"items": [
        {"variant_id": hoodie["m"]["id"], "quantity": 2},
        {"variant_id": hoodie["xxl"]["id"], "quantity": 1},
        {"variant_id": mug["variants"][0]["id"], "quantity": 3},
    ]})
    assert order.status_code == 201, order.text
    body = order.json()

    lines = {item["sku"]: item for item in body["items"]}
    assert {sku: line["product_name"] for sku, line in lines.items()} == {
        "HOOD-BLK-M": "Club Hoodie", "HOOD-BLK-XXL": "Club Hoodie", "MUG-WHT": "Enamel Mug"}
    assert {sku: (line["quantity"], line["unit_price"], line["line_total"]) for sku, line in lines.items()} == {
        "HOOD-BLK-M": (2, 799.0, 1598.0),      # 2 x 799.00
        "HOOD-BLK-XXL": (1, 899.0, 899.0),     # override 1099.00 less the 200.00 member discount
        "MUG-WHT": (3, 249.5, 748.5),          # 3 x 249.50, paise kept exactly
    }
    for line in body["items"]:
        assert Decimal(str(line["line_total"])) == line["quantity"] * Decimal(str(line["unit_price"]))
    # The total is the sum of every line, not the first line's price.
    assert body["total_amount"] == 3245.5 != lines["HOOD-BLK-M"]["line_total"]

    payment = (await client.post(
        f"{PAY}/stripe/create-checkout-session", headers=auth(world.member), json={"merch_order_id": body["id"]})).json()
    assert payment["amount"] == 324550  # paise: exactly 100 x the rupee total
    assert (await pay(world.member, payment)).status_code == 200

    [entry] = await _merch_entries(db)
    assert entry.amount == Decimal("3245.50")
    assert await _stock(db, hoodie["m"]) == 8 and await _stock(db, hoodie["xxl"]) == 0
    assert await _stock(db, mug["variants"][0]) == 2

    # The same variant listed twice is one line with the quantities added.
    merged = (await client.post(f"{MERCH}/orders", headers=auth(world.member), json={"items": [
        {"variant_id": hoodie["m"]["id"], "quantity": 1}, {"variant_id": hoodie["m"]["id"], "quantity": 2}]})).json()
    assert [(item["quantity"], item["line_total"]) for item in merged["items"]] == [(3, 2397.0)]
    assert merged["total_amount"] == 2397.0


async def test_order_pages_show_titles_line_prices_and_total(client, world, auth, hoodie):
    from app.core.config import get_settings as settings
    from app.core.security import create_access_token

    order = (await client.post(f"{MERCH}/orders", headers=auth(world.member), json={"items": [
        {"variant_id": hoodie["m"]["id"], "quantity": 2}, {"variant_id": hoodie["xxl"]["id"], "quantity": 1}]})).json()

    client.cookies.set(settings().session_cookie, create_access_token(world.member.id))
    for path in (f"/checkout/{order['id']}", "/orders", f"/orders/{order['id']}"):
        page = (await client.get(path)).text
        assert "Club Hoodie" in page                                  # product title
        assert "₹799.00" in page and "₹1,598.00" in page              # first line: unit price and line total
        assert "₹899.00" in page                                      # second line is there too
        assert "₹2,497.00" in page                                    # overall total, not the first line's price

    store = (await client.get(f"/store/{hoodie['product']['id']}")).text
    assert "<h1" in store and "Club Hoodie" in store and "₹799.00" in store

    client.cookies.set(settings().session_cookie, create_access_token(world.treasurer.id))
    admin = (await client.get("/admin/merch/orders")).text
    assert "Club Hoodie" in admin and "₹1,598.00" in admin and "₹2,497.00" in admin

