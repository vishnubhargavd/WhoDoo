"""Payments for merchandise orders, behind one provider interface.

`RazorpayProvider` talks to Razorpay (test or live keys). `MockPaymentProvider` is a
Razorpay-compatible stand-in used when no keys are configured: same order ids,
same HMAC signatures, same order -> verify -> ledger flow, just no real money.
Business logic below never checks which provider is active.

Nothing about a payment is trusted from the browser: the amount comes from the
order row, and an order is only fulfilled after its signature is verified here.
"""

import hashlib
import hmac
import json
import logging
import secrets
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.dependencies import CurrentUser
from app.core.errors import AppError, bad_request, conflict, not_found
from app.models.merch import MerchOrder
from app.schemas.payment import PaymentOrderOut, VerifyPayment, WebhookAck
from app.services import audit_service, merch_service

logger = logging.getLogger("whodoo.payments")


def _hmac_hex(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def payment_signature(secret: str, order_id: str, payment_id: str) -> str:
    """Razorpay's checkout signature: HMAC-SHA256 of "order_id|payment_id" with the key secret."""
    return _hmac_hex(secret, f"{order_id}|{payment_id}".encode())


# --- Providers -----------------------------------------------------------------------


class PaymentProvider(Protocol):
    name: str
    is_mock: bool
    key_id: str

    def create_order(self, amount_paise: int, receipt: str, notes: dict[str, str]) -> str: ...

    def verify_payment_signature(self, order_id: str, payment_id: str, signature: str) -> bool: ...

    def verify_webhook_signature(self, body: bytes, signature: str) -> bool: ...

    def refund(self, payment_id: str, amount_paise: int) -> None: ...


class _SignatureChecks:
    """Signature rules shared by both providers; only the secrets differ."""

    key_secret: str
    webhook_secret: str

    def verify_payment_signature(self, order_id: str, payment_id: str, signature: str) -> bool:
        expected = payment_signature(self.key_secret, order_id, payment_id)
        return hmac.compare_digest(expected, signature)

    def verify_webhook_signature(self, body: bytes, signature: str) -> bool:
        if not self.webhook_secret or not signature:
            return False
        return hmac.compare_digest(_hmac_hex(self.webhook_secret, body), signature)


class RazorpayProvider(_SignatureChecks):
    name = "razorpay"
    is_mock = False

    def __init__(self, key_id: str, key_secret: str, webhook_secret: str):
        import razorpay

        self.key_id = key_id
        self.key_secret = key_secret
        self.webhook_secret = webhook_secret
        self._client = razorpay.Client(auth=(key_id, key_secret))

    def create_order(self, amount_paise: int, receipt: str, notes: dict[str, str]) -> str:
        order = self._client.order.create(
            {"amount": amount_paise, "currency": get_settings().currency, "receipt": receipt, "notes": notes}
        )
        return order["id"]

    def refund(self, payment_id: str, amount_paise: int) -> None:
        self._client.payment.refund(payment_id, {"amount": amount_paise})


class MockPaymentProvider(_SignatureChecks):
    """Mock Payment (Razorpay-compatible). For demos without Razorpay keys; moves no money."""

    name = "mock"
    is_mock = True
    key_id = "rzp_mock_key"

    def __init__(self, key_secret: str, webhook_secret: str):
        self.key_secret = key_secret
        self.webhook_secret = webhook_secret

    def create_order(self, amount_paise: int, receipt: str, notes: dict[str, str]) -> str:
        return f"order_mock_{secrets.token_hex(8)}"

    def refund(self, payment_id: str, amount_paise: int) -> None:
        logger.info("Mock refund of %s paise for %s", amount_paise, payment_id)

    def simulate_payment(self, order_id: str, succeed: bool = True) -> dict[str, str]:
        """Play the part of Razorpay Checkout: return a payment id and its signature (bad on failure)."""
        payment_id = f"pay_mock_{secrets.token_hex(8)}"
        signature = payment_signature(self.key_secret, order_id, payment_id) if succeed else "0" * 64
        return {"razorpay_order_id": order_id, "razorpay_payment_id": payment_id, "razorpay_signature": signature}

    def sign_webhook(self, body: bytes) -> str:
        return _hmac_hex(self.webhook_secret, body)


@lru_cache
def get_provider() -> PaymentProvider:
    settings = get_settings()
    if settings.use_razorpay:
        return RazorpayProvider(settings.razorpay_key_id, settings.razorpay_key_secret, settings.razorpay_webhook_secret)
    return MockPaymentProvider(settings.mock_payment_secret, settings.mock_webhook_secret)


# --- Flow ------------------------------------------------------------------------------


@dataclass
class PaymentOutcome:
    """Result of a verification. On failure the order has still been updated, so the caller
    commits first and then raises `error`."""

    order: MerchOrder
    error: AppError | None = None


def _paise(order: MerchOrder) -> int:
    return int(order.total_amount * 100)


async def _order_by_provider_id(session: AsyncSession, provider_order_id: str) -> MerchOrder | None:
    order_id = await session.scalar(
        select(MerchOrder.id).where(MerchOrder.razorpay_order_id == provider_order_id).with_for_update()
    )
    if order_id is None:
        return None
    club_id = await session.scalar(select(MerchOrder.club_id).where(MerchOrder.id == order_id))
    return await merch_service.load_order(session, club_id, order_id, lock=True)


async def create_payment_order(session: AsyncSession, user: CurrentUser, merch_order_id: uuid.UUID) -> PaymentOrderOut:
    """Open (or reopen) a provider order for an unpaid merchandise order the caller owns."""
    provider = get_provider()
    order = await merch_service.load_order(session, user.club_id, merch_order_id, lock=True)
    if order.user_id != user.id:
        raise not_found("Order")
    if order.payment_status == "paid":
        raise conflict("already_paid", "This order has already been paid")
    if order.status != "created":
        raise conflict("order_closed", f"This order is {order.status} and can no longer be paid")
    await merch_service.check_stock(session, order)

    if order.razorpay_order_id is None:
        order.razorpay_order_id = await run_in_threadpool(
            provider.create_order, _paise(order), order.order_number, {"merch_order_id": str(order.id)}
        )
        order.payment_status = "pending"
        await session.flush()
        audit_service.record(
            session, club_id=order.club_id, actor_id=user.id, action="payment.order_created",
            entity_type="merch_order", entity_id=order.id,
            details={"provider": provider.name, "provider_order_id": order.razorpay_order_id, "paise": _paise(order)},
        )
    return PaymentOrderOut(
        merch_order_id=order.id,
        order_number=order.order_number,
        provider=provider.name,
        is_mock=provider.is_mock,
        key_id=provider.key_id,
        razorpay_order_id=order.razorpay_order_id,
        amount=_paise(order),
        currency=get_settings().currency,
    )


async def _fulfil(session: AsyncSession, order: MerchOrder, payment_id: str, actor_id: uuid.UUID) -> PaymentOutcome:
    """Fulfil a verified payment. If stock ran out since the order was placed, refund instead."""
    try:
        await merch_service.fulfil_paid_order(session, order, payment_id, actor_id=actor_id)
        return PaymentOutcome(order)
    except merch_service.OutOfStock as sold_out:
        order = await merch_service.load_order(session, order.club_id, order.id, lock=True)
        order.payment_status = "refunded"
        order.status = "cancelled"
        order.razorpay_payment_id = payment_id
        await session.flush()
        audit_service.record(
            session, club_id=order.club_id, actor_id=actor_id, action="payment.refunded_out_of_stock",
            entity_type="merch_order", entity_id=order.id, details={"payment_id": payment_id, "sku": sold_out.sku},
        )
        await run_in_threadpool(get_provider().refund, payment_id, _paise(order))
        return PaymentOutcome(
            order,
            conflict("out_of_stock", "This item sold out before your payment went through. You have been refunded."),
        )


async def verify_payment(session: AsyncSession, user: CurrentUser, data: VerifyPayment) -> PaymentOutcome:
    order = await _order_by_provider_id(session, data.razorpay_order_id)
    if order is None or order.user_id != user.id:
        raise not_found("Order")

    valid = get_provider().verify_payment_signature(
        data.razorpay_order_id, data.razorpay_payment_id, data.razorpay_signature
    )
    if not valid:
        merch_service.mark_payment_failed(session, order, "invalid_signature", actor_id=user.id)
        await session.flush()
        return PaymentOutcome(
            order, bad_request("payment_verification_failed", "The payment could not be verified. You were not charged.")
        )
    if order.payment_status == "paid":
        if order.razorpay_payment_id != data.razorpay_payment_id:
            return PaymentOutcome(order, conflict("already_paid", "This order was already paid with another payment"))
        return PaymentOutcome(order)
    return await _fulfil(session, order, data.razorpay_payment_id, user.id)


async def handle_webhook(session: AsyncSession, body: bytes, signature: str) -> WebhookAck:
    """Razorpay webhook: the same fulfilment as /verify, for when the browser never comes back."""
    if not get_provider().verify_webhook_signature(body, signature):
        raise AppError(401, "invalid_webhook_signature", "Webhook signature is missing or wrong")
    try:
        event = json.loads(body)
        kind = event["event"]
        payment = event["payload"]["payment"]["entity"]
        provider_order_id, payment_id = payment["order_id"], payment["id"]
    except (ValueError, KeyError, TypeError):
        return WebhookAck(status="ignored", detail="Not a payment event")

    order = await _order_by_provider_id(session, provider_order_id)
    if order is None:
        return WebhookAck(status="ignored", detail="Unknown order")

    if kind in ("payment.captured", "order.paid"):
        if int(payment.get("amount", -1)) != _paise(order):
            audit_service.record(
                session, club_id=order.club_id, actor_id=None, action="payment.amount_mismatch",
                entity_type="merch_order", entity_id=order.id,
                details={"expected_paise": _paise(order), "got": payment.get("amount"), "payment_id": payment_id},
            )
            return WebhookAck(status="ignored", detail="Amount does not match the order")
        if order.payment_status == "paid":
            return WebhookAck(status="processed", detail="Already paid")
        if order.status != "created":
            # Money arrived for an order we already closed (e.g. cancelled): send it back.
            audit_service.record(
                session, club_id=order.club_id, actor_id=None, action="payment.refunded_closed_order",
                entity_type="merch_order", entity_id=order.id, details={"payment_id": payment_id, "status": order.status},
            )
            await run_in_threadpool(get_provider().refund, payment_id, _paise(order))
            return WebhookAck(status="processed", detail=f"Order is {order.status}; payment refunded")
        outcome = await _fulfil(session, order, payment_id, order.user_id)
        return WebhookAck(status="processed", detail=outcome.error.code if outcome.error else "Order paid")
    if kind == "payment.failed":
        merch_service.mark_payment_failed(session, order, "provider_reported_failure", actor_id=None)
        await session.flush()
        return WebhookAck(status="processed", detail="Payment failed")
    return WebhookAck(status="ignored", detail=f"Unhandled event {kind}")


async def simulate_mock_payment(
    session: AsyncSession, user: CurrentUser, provider_order_id: str, succeed: bool
) -> dict[str, str]:
    """Mock provider only: stand in for the Razorpay Checkout popup."""
    provider = get_provider()
    if not isinstance(provider, MockPaymentProvider):
        raise not_found("Mock payments")
    owner = await session.scalar(select(MerchOrder.user_id).where(MerchOrder.razorpay_order_id == provider_order_id))
    if owner != user.id:
        raise not_found("Order")
    return provider.simulate_payment(provider_order_id, succeed)
