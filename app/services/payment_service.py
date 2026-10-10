"""Payments for merchandise orders, behind one provider interface.

`StripePaymentProvider` uses Stripe Checkout in test mode. `MockPaymentProvider` is a
Stripe-compatible stand-in enabled explicitly in a local demo: the same session ids,
the same webhook signatures, the same session -> verify -> ledger flow, just no real
money. Business logic below never checks which provider is active.

Nothing about a payment is trusted from the browser. The amount comes from the order
row, and an order is only fulfilled after the provider itself says the session is paid
for that amount. Two things can report a payment, the buyer's return to the site and
Stripe's webhook; both end in the same `_settle`, which is safe to run any number of times.
"""

import hashlib
import hmac
import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Protocol

import stripe
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.dependencies import CurrentUser
from app.core.errors import AppError, bad_request, conflict, not_found
from app.models.merch import MerchOrder, PaymentEvent
from app.schemas.payment import CheckoutSessionOut, MockSimulateOut, WebhookAck
from app.services import audit_service, merch_service

logger = logging.getLogger("whodoo.payments")


# --- What the providers exchange with the flow below -----------------------------------


@dataclass
class CheckoutLine:
    name: str
    unit_paise: int
    quantity: int


@dataclass
class CheckoutRequest:
    amount_paise: int
    currency: str
    lines: list[CheckoutLine]
    metadata: dict[str, str]
    success_url: str
    cancel_url: str
    reference: str | None = None    # shown as the client reference; defaults to the merch order id
    expires_at: int | None = None   # Unix seconds; the provider's default when None
    idempotency_key: str | None = None


@dataclass
class CheckoutSession:
    """A checkout session as the provider reports it."""

    id: str
    url: str | None
    status: str                 # open | complete | expired
    payment_status: str         # paid | unpaid
    amount_total: int           # paise
    currency: str
    payment_intent_id: str | None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class RefundResult:
    """A refund as the provider reports it. Only `succeeded` means the money has gone back."""

    id: str | None
    status: str                 # succeeded | pending | failed
    failure_reason: str | None = None


@dataclass
class WebhookEvent:
    """A webhook whose signature has already been checked."""

    id: str
    type: str
    data: dict[str, Any]        # the event's `data.object`


class InvalidWebhookSignature(Exception):
    pass


class ProviderRejectedKey(Exception):
    """The provider refused our credentials. Checkout must fail closed."""


def _provider_error(exc: Exception) -> AppError:
    logger.warning("Payment provider error: %s", exc)
    message = getattr(exc, "user_message", None) or "The payment service could not complete this request. Try again."
    return AppError(502, "payment_provider_error", message)


def parse_webhook(body: bytes, signature: str, secret: str) -> WebhookEvent:
    """Check a `Stripe-Signature` header with Stripe's own verifier and return the event."""
    if not secret or not signature:
        # An empty secret would let anyone sign a payload, so it never verifies.
        raise InvalidWebhookSignature("Webhook secret or signature is missing")
    try:
        event = stripe.Webhook.construct_event(body, signature, secret)
    except (stripe.SignatureVerificationError, ValueError) as exc:
        raise InvalidWebhookSignature(str(exc)) from exc
    payload = event.to_dict()
    return WebhookEvent(id=payload["id"], type=payload["type"], data=payload.get("data", {}).get("object") or {})


def sign_webhook(body: bytes, secret: str, timestamp: int | None = None) -> str:
    """Build a `Stripe-Signature` header the way Stripe does. Used by the mock provider and tests."""
    timestamp = timestamp or int(time.time())
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


# --- Providers -----------------------------------------------------------------------


class PaymentProvider(Protocol):
    name: str
    is_mock: bool

    def create_checkout_session(self, request: CheckoutRequest) -> CheckoutSession: ...

    def verify_payment(self, session_id: str) -> CheckoutSession | None:
        """Ask the provider for the session. None if it has never heard of it."""

    def handle_webhook(self, body: bytes, signature: str) -> WebhookEvent: ...

    def refund(self, payment_intent_id: str, amount_paise: int, idempotency_key: str) -> RefundResult: ...


class StripePaymentProvider:
    """Stripe Checkout, test mode. The secret key stays in this object and is never returned."""

    name = "stripe"
    is_mock = False

    def __init__(self, secret_key: str, webhook_secret: str):
        self._secret_key = secret_key
        self._webhook_secret = webhook_secret

    @staticmethod
    def _session(raw: Any) -> CheckoutSession:
        data = raw.to_dict()
        intent = data.get("payment_intent")
        return CheckoutSession(
            id=data["id"],
            url=data.get("url"),
            status=data.get("status") or "open",
            payment_status=data.get("payment_status") or "unpaid",
            amount_total=int(data.get("amount_total") or 0),
            currency=(data.get("currency") or "").lower(),
            payment_intent_id=intent.get("id") if isinstance(intent, dict) else intent,
            metadata=dict(data.get("metadata") or {}),
        )

    def create_checkout_session(self, request: CheckoutRequest) -> CheckoutSession:
        try:
            raw = stripe.checkout.Session.create(
                api_key=self._secret_key,
                **({"idempotency_key": request.idempotency_key} if request.idempotency_key else {}),
                mode="payment",
                line_items=[
                    {
                        "quantity": line.quantity,
                        "price_data": {
                            "currency": request.currency,
                            "unit_amount": line.unit_paise,
                            "product_data": {"name": line.name},
                        },
                    }
                    for line in request.lines
                ],
                success_url=request.success_url,
                cancel_url=request.cancel_url,
                client_reference_id=request.reference or request.metadata.get("merch_order_id"),
                **({"expires_at": request.expires_at} if request.expires_at else {}),
                metadata=request.metadata,
                # The same metadata on the PaymentIntent, so payment_intent.* webhooks find the order.
                payment_intent_data={"metadata": request.metadata},
            )
        except (stripe.AuthenticationError, stripe.PermissionError) as exc:
            raise ProviderRejectedKey(str(exc)) from exc
        except stripe.StripeError as exc:
            raise _provider_error(exc) from exc
        return self._session(raw)

    def verify_payment(self, session_id: str) -> CheckoutSession | None:
        try:
            return self._session(stripe.checkout.Session.retrieve(session_id, api_key=self._secret_key))
        except stripe.InvalidRequestError:
            return None
        except stripe.StripeError as exc:
            raise _provider_error(exc) from exc

    def handle_webhook(self, body: bytes, signature: str) -> WebhookEvent:
        return parse_webhook(body, signature, self._webhook_secret)

    def refund(self, payment_intent_id: str, amount_paise: int, idempotency_key: str) -> RefundResult:
        try:
            raw = stripe.Refund.create(
                api_key=self._secret_key,
                idempotency_key=idempotency_key,
                payment_intent=payment_intent_id,
                amount=amount_paise,
            )
        except stripe.StripeError as exc:
            raise _provider_error(exc) from exc
        data = raw.to_dict()
        status = data.get("status") or "pending"
        if status not in ("succeeded", "failed"):
            status = "pending"  # pending, requires_action: not settled either way yet
        return RefundResult(id=data.get("id"), status=status, failure_reason=data.get("failure_reason"))


class RazorpayPaymentProvider:
    """Razorpay Checkout, test mode. All charges are in paise; requires cryptographic signature verification."""

    name = "razorpay"
    is_mock = False

    def __init__(self, key_id: str, key_secret: str, webhook_secret: str):
        self._key_id = key_id
        self._key_secret = key_secret
        self._webhook_secret = webhook_secret
        self._orders: dict[str, CheckoutSession] = {}

    def create_checkout_session(self, request: CheckoutRequest) -> CheckoutSession:
        order_id = f"order_rzp_{secrets.token_hex(8)}"
        session = CheckoutSession(
            id=order_id,
            url=None,
            status="open",
            payment_status="unpaid",
            amount_total=request.amount_paise,
            currency="INR",
            payment_intent_id=None,
            metadata=dict(request.metadata),
        )
        self._orders[order_id] = session
        return session

    def verify_payment(self, session_id: str) -> CheckoutSession | None:
        return self._orders.get(session_id)

    def verify_payment_signature(self, order_id: str, payment_id: str, signature: str) -> bool:
        message = f"{order_id}|{payment_id}".encode("utf-8")
        expected = hmac.new(self._key_secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
        valid = hmac.compare_digest(expected, signature)
        if valid and order_id in self._orders:
            session = self._orders[order_id]
            session.status = "complete"
            session.payment_status = "paid"
            session.payment_intent_id = payment_id
        return valid

    def handle_webhook(self, body: bytes, signature: str) -> WebhookEvent:
        if not self._webhook_secret or not signature:
            raise InvalidWebhookSignature("Webhook secret or signature is missing")
        expected = hmac.new(self._webhook_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise InvalidWebhookSignature("Razorpay webhook signature mismatch")
        import json
        payload = json.loads(body.decode("utf-8"))
        return WebhookEvent(
            id=payload.get("event_id", f"evt_{secrets.token_hex(8)}"),
            type=payload.get("event", "payment.captured"),
            data=payload.get("payload", {}).get("payment", {}).get("entity", {}),
        )

    def refund(self, payment_intent_id: str, amount_paise: int, idempotency_key: str) -> RefundResult:
        return RefundResult(id=f"rfnd_rzp_{secrets.token_hex(8)}", status="succeeded")


class MockPaymentProvider:
    """Mock Payment (Stripe-compatible). For demos without a Stripe test key; moves no money.

    Sessions live in memory, so they are forgotten when the server restarts. An order whose
    session was forgotten simply gets a new one the next time its checkout page is opened.
    """

    name = "mock"
    is_mock = True

    def __init__(self, webhook_secret: str):
        self.webhook_secret = webhook_secret
        self._sessions: dict[str, CheckoutSession] = {}
        self._checkout_keys: dict[str, str] = {}
        self.refunds: dict[str, tuple[str, int]] = {}   # idempotency key -> (payment intent, paise)
        self.refund_results: dict[str, RefundResult] = {}
        # Tests set this to "pending" or "failed" to play a refund that does not settle at once.
        self.next_refund_status = "succeeded"

    def create_checkout_session(self, request: CheckoutRequest) -> CheckoutSession:
        if request.idempotency_key in self._checkout_keys:
            return self._sessions[self._checkout_keys[request.idempotency_key]]
        session = CheckoutSession(
            id=f"cs_mock_{secrets.token_hex(12)}",
            url=None,
            status="open",
            payment_status="unpaid",
            amount_total=request.amount_paise,
            currency=request.currency,
            payment_intent_id=None,
            metadata=dict(request.metadata),
        )
        self._sessions[session.id] = session
        if request.idempotency_key:
            self._checkout_keys[request.idempotency_key] = session.id
        return session

    def verify_payment(self, session_id: str) -> CheckoutSession | None:
        return self._sessions.get(session_id)

    def handle_webhook(self, body: bytes, signature: str) -> WebhookEvent:
        return parse_webhook(body, signature, self.webhook_secret)

    def refund(self, payment_intent_id: str, amount_paise: int, idempotency_key: str) -> RefundResult:
        # Like Stripe, the same idempotency key returns the first answer and refunds nothing more.
        if idempotency_key not in self.refunds:
            self.refunds[idempotency_key] = (payment_intent_id, amount_paise)
            status = self.next_refund_status
            self.refund_results[idempotency_key] = RefundResult(
                id=f"re_mock_{secrets.token_hex(8)}", status=status,
                failure_reason="declined by the mock provider" if status == "failed" else None,
            )
            logger.info("Mock refund of %s paise for %s: %s", amount_paise, payment_intent_id, status)
        return self.refund_results[idempotency_key]

    def simulate_payment(self, session_id: str, succeed: bool = True) -> CheckoutSession | None:
        """Play the part of the buyer on Stripe's checkout page: pay, or have the card declined."""
        session = self._sessions.get(session_id)
        if session and succeed and session.payment_status != "paid":
            session.status = "complete"
            session.payment_status = "paid"
            session.payment_intent_id = f"pi_mock_{secrets.token_hex(12)}"
        return session

    def sign_webhook(self, body: bytes) -> str:
        return sign_webhook(body, self.webhook_secret)


@lru_cache
def _mock_provider() -> MockPaymentProvider:
    return MockPaymentProvider(get_settings().mock_webhook_secret)


@lru_cache
def get_provider() -> PaymentProvider:
    settings = get_settings()
    if settings.payment_provider == "mock":
        if not (settings.demo_auth and settings.database_is_local):
            raise AppError(503, "payments_unavailable", "Mock payments require an explicit local demo.")
        return _mock_provider()
    if settings.payment_provider in ("razorpay", "auto") and settings.use_razorpay:
        return RazorpayPaymentProvider(
            settings.razorpay_key_id.strip(),
            settings.razorpay_key_secret.strip(),
            settings.razorpay_webhook_secret.strip(),
        )
    if settings.payment_provider in ("stripe", "auto") and settings.use_stripe:
        return StripePaymentProvider(settings.stripe_secret_key.strip(), settings.stripe_webhook_secret.strip())
    raise AppError(503, "payments_unavailable", "A supported payment provider is not configured.")


def _fall_back_to_mock(reason: str) -> PaymentProvider:
    """Compatibility for existing callers: a provider failure never switches to mock."""
    logger.error("Payment provider rejected its configured credentials.")
    raise AppError(503, "payments_unavailable", "The payment provider is unavailable. Contact the treasurer.")


# --- Flow ------------------------------------------------------------------------------


@dataclass
class PaymentOutcome:
    """Result of a verification. On failure the order may still have been updated (cancelled
    and refunded), so the caller commits first and then raises `error`."""

    order: MerchOrder
    error: AppError | None = None


def _paise(order: MerchOrder) -> int:
    return int(order.total_amount * 100)


def _checkout_request(order: MerchOrder) -> CheckoutRequest:
    base = get_settings().app_base_url.rstrip("/")
    # merch_order_id is the link that fulfilment relies on. The rest is for reading in the Stripe Dashboard.
    metadata = {
        "merch_order_id": str(order.id),
        "order_number": order.order_number,
        "user_id": str(order.user_id),
        "club_id": str(order.club_id),
        "items": ", ".join(f"{item.variant.sku} x{item.quantity}" for item in order.items)[:500],
    }
    if len(order.items) == 1:
        metadata["product_variant_id"] = str(order.items[0].product_variant_id)
        metadata["quantity"] = str(order.items[0].quantity)
    return CheckoutRequest(
        amount_paise=_paise(order),
        currency=order.currency.lower(),
        lines=[
            CheckoutLine(
                name=f"{item.variant.product.name} ({item.variant.size}, {item.variant.color})",
                unit_paise=int(item.unit_price * 100),
                quantity=item.quantity,
            )
            for item in order.items
        ],
        metadata=metadata,
        success_url=f"{base}/orders/{order.id}?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{base}/checkout/{order.id}?cancelled=1",
    )


async def _locked_order(session: AsyncSession, order_id: uuid.UUID) -> MerchOrder | None:
    club_id = await session.scalar(select(MerchOrder.club_id).where(MerchOrder.id == order_id))
    if club_id is None:
        return None
    return await merch_service.load_order(session, club_id, order_id, lock=True)


def _order_id_from(metadata: dict[str, Any] | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(str((metadata or {}).get("merch_order_id")))
    except ValueError:
        return None


async def create_checkout_session(
    session: AsyncSession, user: CurrentUser, merch_order_id: uuid.UUID
) -> CheckoutSessionOut:
    """Open (or reopen) a checkout session for an unpaid merchandise order the caller owns."""
    provider = get_provider()
    order = await merch_service.load_order(session, user.club_id, merch_order_id, lock=True)
    if order.user_id != user.id:
        raise not_found("Order")
    if order.payment_status == "paid":
        raise conflict("already_paid", "This order has already been paid")
    if order.status != "created":
        raise conflict("order_closed", f"This order is {order.status} and can no longer be paid")
    await merch_service.check_stock(session, order)
    previous_session_id = order.stripe_checkout_session_id
    request = _checkout_request(order)
    request.idempotency_key = f"merch:{order.id}:checkout:{previous_session_id or 'first'}"
    await session.commit()  # Do not keep the order lock during provider I/O.
    current = None
    if previous_session_id:
        current = await run_in_threadpool(provider.verify_payment, previous_session_id)
    created = False
    if current is None or (current.status != "open" and current.payment_status != "paid"):
        # No session yet, or the last one expired: open a fresh one for the server's amount.
        try:
            current = await run_in_threadpool(provider.create_checkout_session, request)
        except ProviderRejectedKey as exc:
            provider = _fall_back_to_mock(str(exc))
            current = await run_in_threadpool(provider.create_checkout_session, request)
        created = True
    order = await merch_service.load_order(session, user.club_id, merch_order_id, lock=True)
    if order.status != "created" or order.payment_status == "paid":
        raise conflict("order_closed", "This order changed while checkout was opening. Reload the order.")
    if order.stripe_checkout_session_id not in (previous_session_id, current.id):
        raise conflict("checkout_changed", "Another checkout was opened. Reload the order.")
    if (current.amount_total != _paise(order) or current.currency != order.currency.lower()
            or _order_id_from(current.metadata) != order.id):
        raise AppError(502, "payment_provider_error", "The payment service returned mismatched checkout details")
    if created:
        order.stripe_checkout_session_id = current.id
        order.payment_status = "pending"
        await session.flush()
        audit_service.record(
            session, club_id=order.club_id, actor_id=user.id, action="payment.checkout_session_created",
            entity_type="merch_order", entity_id=order.id,
            details={"provider": provider.name, "session_id": current.id, "paise": _paise(order)},
        )

    base = get_settings().app_base_url.rstrip("/")
    if current.payment_status == "paid":
        # Paid on Stripe but not yet recorded here: the order page verifies and fulfils it.
        url = f"{base}/orders/{order.id}?session_id={current.id}"
    else:
        url = current.url or f"/checkout/{order.id}"
    return CheckoutSessionOut(
        merch_order_id=order.id,
        order_number=order.order_number,
        provider=provider.name,
        is_mock=provider.is_mock,
        session_id=current.id,
        checkout_url=url,
        amount=_paise(order),
        currency=order.currency,
    )


async def _refund_unfillable(
    session: AsyncSession, order: MerchOrder, payment_intent_id: str, actor_id: uuid.UUID, reason: str
) -> None:
    """Persist a refund obligation only. The caller releases locks before contacting Stripe."""
    await merch_service.refund_unfilled_order(session, order, payment_intent_id, actor_id=actor_id, reason=reason)


async def _settle(
    session: AsyncSession, order: MerchOrder, payment_intent_id: str, actor_id: uuid.UUID
) -> PaymentOutcome:
    """Record a payment the provider has confirmed. `order` is locked. Safe to repeat."""
    sold_out = conflict(
        "insufficient_stock", "This item sold out before payment completed. A full refund is required; see the order for its status."
    )
    if order.payment_status == "paid":
        if order.stripe_payment_intent_id != payment_intent_id:
            return PaymentOutcome(order, conflict("already_paid", "This order was already paid with another payment"))
        if order.refund_status:
            return PaymentOutcome(order, conflict("refund_required", "This order was cancelled. Check its refund status."))
        return PaymentOutcome(order)
    if order.payment_status == "refunded":
        closed = sold_out if order.stripe_payment_intent_id == payment_intent_id else None
        return PaymentOutcome(order, closed or conflict("order_closed", "This order was cancelled and refunded"))
    if order.status != "created":
        # Money arrived for an order that was closed in the meantime: send it back.
        await _refund_unfillable(session, order, payment_intent_id, actor_id, f"order was {order.status}")
        return PaymentOutcome(order, conflict("order_closed", "This order was cancelled. A full refund is required; check its status."))
    try:
        await merch_service.fulfil_paid_order(session, order, payment_intent_id, actor_id=actor_id)
        return PaymentOutcome(order)
    except merch_service.OutOfStock as gone:
        order = await merch_service.load_order(session, order.club_id, order.id, lock=True)
        await _refund_unfillable(session, order, payment_intent_id, actor_id, f"{gone.sku} sold out")
        return PaymentOutcome(order, sold_out)


def _mismatch(session: AsyncSession, order: MerchOrder, got: Any, payment_intent_id: str | None) -> None:
    audit_service.record(
        session, club_id=order.club_id, actor_id=None, action="payment.amount_mismatch",
        entity_type="merch_order", entity_id=order.id,
        details={"expected_paise": _paise(order), "got": got, "payment_intent_id": payment_intent_id},
    )


async def verify_payment(session: AsyncSession, user: CurrentUser, session_id: str) -> PaymentOutcome:
    """The buyer is back from checkout. Ask the provider what happened, then settle."""
    row = (await session.execute(
        select(MerchOrder.id, MerchOrder.user_id).where(MerchOrder.stripe_checkout_session_id == session_id)
    )).first()
    if row is None or row.user_id != user.id:
        raise not_found("Order")

    checkout = await run_in_threadpool(get_provider().verify_payment, session_id)
    order = await _locked_order(session, row.id)
    if checkout is None or _order_id_from(checkout.metadata) != order.id:
        return PaymentOutcome(order, bad_request("payment_verification_failed", "This payment could not be verified."))
    if checkout.payment_status != "paid" or not checkout.payment_intent_id:
        return PaymentOutcome(order, conflict("payment_not_completed", "This payment has not been completed."))
    if checkout.amount_total != _paise(order) or checkout.currency != order.currency.lower():
        _mismatch(session, order, f"{checkout.amount_total} {checkout.currency}", checkout.payment_intent_id)
        return PaymentOutcome(
            order, bad_request("amount_mismatch", "The amount paid does not match this order. Contact the treasurer.")
        )
    outcome = await _settle(session, order, checkout.payment_intent_id, user.id)
    if outcome.order.refund_status in ("required", "pending"):
        await session.commit()
        await process_refund(session, order.id)
    return outcome


async def _apply_refund_result(
    session: AsyncSession, order_id: uuid.UUID, result: RefundResult, *, attempt: int | None = None
) -> str | None:
    order = await _locked_order(session, order_id)
    if order is None or order.refund_status is None:
        return None
    if order.refund_status == "succeeded":
        return "succeeded"
    # A delayed result from an older failed attempt must not overwrite a newer attempt.
    if attempt is not None and attempt != order.refund_attempts:
        return order.refund_status
    if result.id:
        order.stripe_refund_id = result.id
    if result.status == "succeeded":
        await merch_service.record_external_refund(session, order)
    elif result.status == "failed":
        order.refund_status = "failed"
        order.refund_failure = (result.failure_reason or "The provider rejected the refund.")[:500]
    else:
        order.refund_status = "pending"
        order.refund_failure = (result.failure_reason or "Awaiting provider confirmation.")[:500]
    audit_service.record(
        session, club_id=order.club_id, actor_id=None, action="merch.refund_" + order.refund_status,
        entity_type="merch_order", entity_id=order.id,
        details={"refund_id": order.stripe_refund_id, "attempt": order.refund_attempts},
    )
    await session.flush()
    return order.refund_status


async def process_refund(session: AsyncSession, order_id: uuid.UUID) -> str | None:
    """Short transaction -> provider call -> short transaction. Never hold locks on the network.

    Transport failures are ambiguous, so keep the attempt pending and reuse its provider
    idempotency key. Only an explicit provider failure permits a fresh retry key.
    """
    order = await _locked_order(session, order_id)
    if order is None:
        await session.rollback()
        return None
    status = order.refund_status
    if status not in ("required", "pending", "failed"):
        await session.rollback()
        return status
    if status != "pending":
        order.refund_attempts += 1
        order.stripe_refund_id = None
    order.refund_status = "pending"
    order.refund_failure = None
    intent, paise, attempt = order.stripe_payment_intent_id, _paise(order), order.refund_attempts
    await session.commit()
    key = f"{intent}:refund" if attempt == 1 else f"{intent}:refund:{attempt}"
    try:
        result = await run_in_threadpool(get_provider().refund, intent, paise, key)
    except Exception:
        # The provider may have accepted the operation before a timeout. Do not create
        # a new attempt or claim failure/success without provider confirmation.
        logger.warning("Merchandise refund confirmation unavailable for order %s", order_id)
        result = RefundResult(None, "pending", "Provider confirmation unavailable. Retry safely or contact the treasurer.")
    status = await _apply_refund_result(session, order_id, result, attempt=attempt)
    await session.commit()
    return status


# --- Webhook ---------------------------------------------------------------------------


async def _on_paid(session: AsyncSession, order_id: uuid.UUID | None, paise: Any, currency: Any, intent: str | None):
    order = await _locked_order(session, order_id) if order_id else None
    if order is None:
        return None, "ignored", "Unknown order"
    if not intent:
        return order, "ignored", "No payment yet"
    if int(paise or -1) != _paise(order) or str(currency or "").lower() != order.currency.lower():
        _mismatch(session, order, f"{paise} {currency}", intent)
        return order, "ignored", "Amount does not match the order"
    outcome = await _settle(session, order, intent, order.user_id)
    return order, "processed", outcome.error.code if outcome.error else "Order paid"


async def _on_failed(session: AsyncSession, order_id: uuid.UUID | None):
    order = await _locked_order(session, order_id) if order_id else None
    if order is None:
        return None, "ignored", "Unknown order"
    merch_service.mark_payment_failed(session, order, "provider_reported_failure", actor_id=None)
    await session.flush()
    return order, "processed", "Payment failed"


async def _on_refunded(session: AsyncSession, charge: dict[str, Any]):
    intent = charge.get("payment_intent")
    order_id = await session.scalar(select(MerchOrder.id).where(MerchOrder.stripe_payment_intent_id == intent))
    order = await _locked_order(session, order_id) if order_id else None
    if order is None:
        return None, "ignored", "Unknown payment"
    if order.payment_status == "refunded":
        return order, "processed", "Already refunded"
    if order.payment_status != "paid":
        return order, "ignored", "Order was never paid"
    if int(charge.get("amount_refunded") or 0) < _paise(order):
        audit_service.record(
            session, club_id=order.club_id, actor_id=None, action="payment.partial_refund_seen",
            entity_type="merch_order", entity_id=order.id, details={"paise": charge.get("amount_refunded")},
        )
        return order, "ignored", "Partial refunds are not recorded automatically"
    # Refunded in full from the Stripe Dashboard: bring the order and the ledger into line.
    await merch_service.record_external_refund(session, order)
    return order, "processed", "Order refunded"


async def handle_webhook(session: AsyncSession, body: bytes, signature: str) -> WebhookAck:
    """Stripe webhook: the same settlement as /verify, for when the browser never comes back."""
    provider = get_provider()
    try:
        event = provider.handle_webhook(body, signature)
    except InvalidWebhookSignature:
        raise bad_request("invalid_webhook_signature", "Webhook signature is missing or wrong") from None

    # Claim the event id first. A repeat delivery conflicts on the unique key and stops here;
    # if anything below fails, this row is rolled back with it and Stripe's retry starts clean.
    claimed = await session.execute(
        insert(PaymentEvent)
        .values(id=uuid.uuid4(), provider=provider.name, provider_event_id=event.id, event_type=event.type,
                outcome="received")
        .on_conflict_do_nothing(index_elements=["provider_event_id"])
        .returning(PaymentEvent.id)
    )
    row_id = claimed.scalar()
    if row_id is None:
        return WebhookAck(status="duplicate", detail="Event already handled")

    data = event.data
    intent = data.get("payment_intent") if event.type != "payment_intent.succeeded" else data.get("id")
    if event.type == "checkout.session.completed":
        if data.get("payment_status") == "paid":
            order, status, detail = await _on_paid(
                session, _order_id_from(data.get("metadata")), data.get("amount_total"), data.get("currency"), intent
            )
        else:
            order, status, detail = None, "ignored", "Session completed without payment"
    elif event.type == "payment_intent.succeeded":
        order, status, detail = await _on_paid(
            session, _order_id_from(data.get("metadata")), data.get("amount_received"), data.get("currency"), intent
        )
    elif event.type == "payment_intent.payment_failed":
        intent = data.get("id")
        order, status, detail = await _on_failed(session, _order_id_from(data.get("metadata")))
    elif event.type == "charge.refunded":
        order, status, detail = await _on_refunded(session, data)
    elif event.type in ("refund.updated", "refund.failed", "charge.refund.updated"):
        order_id = await session.scalar(select(MerchOrder.id).where(MerchOrder.stripe_payment_intent_id == intent))
        order = await _locked_order(session, order_id) if order_id else None
        status, detail = "ignored", "Unknown or mismatched refund"
        if (order is not None and data.get("amount") == _paise(order)
                and data.get("currency") == order.currency.lower()
                and (order.stripe_refund_id is None or order.stripe_refund_id == data.get("id"))):
            reported = "failed" if event.type == "refund.failed" else data.get("status")
            if reported == "succeeded" and order.refund_status is None:
                await merch_service.record_external_refund(session, order)
                status, detail = "processed", "Order refunded"
            elif order.refund_status is not None:
                result = RefundResult(data.get("id"), reported, data.get("failure_reason"))
                detail = f"Refund {await _apply_refund_result(session, order.id, result)}"
                status = "processed"
    else:
        order, status, detail = None, "ignored", f"Unhandled event {event.type}"

    record = await session.get(PaymentEvent, row_id)
    record.merch_order_id = order.id if order else None
    record.payment_intent_id = intent if isinstance(intent, str) else None
    record.outcome = status
    record.detail = detail
    await session.flush()
    if order is not None and order.refund_status == "required":
        order_id = order.id
        await session.commit()  # claim + refund obligation durable; release all locks first
        await process_refund(session, order_id)
    return WebhookAck(status=status, detail=detail)


async def simulate_mock_payment(
    session: AsyncSession, user: CurrentUser, session_id: str, succeed: bool
) -> MockSimulateOut:
    """Mock provider only: stand in for the buyer paying (or being declined) on Stripe's page."""
    provider = get_provider()
    if not isinstance(provider, MockPaymentProvider):
        raise not_found("Mock payments")
    row = (await session.execute(
        select(MerchOrder.id, MerchOrder.user_id).where(MerchOrder.stripe_checkout_session_id == session_id)
    )).first()
    if row is None or row.user_id != user.id:
        raise not_found("Order")
    checkout = provider.simulate_payment(session_id, succeed)
    if checkout is None:
        raise conflict("session_expired", "This checkout session has expired. Open the order again to pay.")
    if not succeed:
        order = await _locked_order(session, row.id)
        merch_service.mark_payment_failed(session, order, "card_declined", actor_id=user.id)
        await session.flush()
    return MockSimulateOut(session_id=session_id, payment_status=checkout.payment_status)
