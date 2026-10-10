"""Razorpay, TEST MODE ONLY: Payment Links for collections, RazorpayX for reimbursement payouts.

`RazorpayPaymentProvider` speaks the same `PaymentProvider` protocol as Stripe and the mock
(app/services/payment_service.py), so merchandise, membership dues and FairSeat tickets use it
without knowing it is Razorpay. A Razorpay Payment Link plays the part of a checkout session:

    checkout session id   -> payment link id (plink_...)
    payment intent id     -> payment id (pay_...)
    metadata              -> notes

Nothing is trusted from the browser or from a webhook body alone. A webhook must carry a valid
`X-Razorpay-Signature` (HMAC-SHA256 of the raw body with the webhook secret), and every module
reads the payment link back from the API before it records anything (`verify_payment`).

Status of this code: written against Razorpay's documented REST API and exercised in tests with
a fake HTTP transport. It has NOT been run against a real Razorpay test account; see
README_BATCH_A.md for what to check before switching it on.
"""

import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from app.core.config import get_settings
from app.core.errors import AppError
from app.services.payment_service import (
    CheckoutRequest,
    CheckoutSession,
    InvalidWebhookSignature,
    ProviderRejectedKey,
    RefundResult,
    WebhookEvent,
)

logger = logging.getLogger("whodoo.razorpay")

PAYMENT_LINK_ID = re.compile(r"^plink_[A-Za-z0-9]{6,40}$")
MIN_LINK_SECONDS = 16 * 60  # Razorpay wants expire_by at least 15 minutes ahead
MAX_NOTES = 15


def verify_signature(body: bytes, signature: str, secret: str) -> None:
    """Razorpay webhook signature: hex HMAC-SHA256 of the raw body. Constant-time comparison."""
    if not secret or not signature:
        raise InvalidWebhookSignature("Webhook secret or signature is missing")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature.strip()):
        raise InvalidWebhookSignature("Signature does not match")


def sign(body: bytes, secret: str) -> str:
    """Build an `X-Razorpay-Signature` value. For tests and local simulation."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _error(exc: Exception | str) -> AppError:
    logger.warning("Razorpay error: %s", exc)
    return AppError(502, "payment_provider_error", "The payment service could not complete this request. Try again.")


class _Api:
    """A tiny Razorpay REST client. Basic auth with the key pair; the secret never leaves here."""

    def __init__(self, key_id: str, key_secret: str, base: str, transport: httpx.BaseTransport | None = None):
        self._client = httpx.Client(
            base_url=base.rstrip("/"), auth=(key_id, key_secret), timeout=httpx.Timeout(10.0, connect=3.0),
            transport=transport,
        )

    def call(self, method: str, path: str, *, json_body: dict | None = None, headers: dict | None = None,
             missing_ok: bool = False) -> dict | None:
        try:
            response = self._client.request(method, path, json=json_body, headers=headers)
        except httpx.HTTPError as exc:
            raise _error(exc) from exc
        if response.status_code == 401:
            raise ProviderRejectedKey("Razorpay rejected the key pair")
        if response.status_code == 404 and missing_ok:
            return None
        if response.status_code >= 400:
            try:
                description = response.json().get("error", {}).get("description")
            except ValueError:
                description = None
            raise _error(f"{method} {path}: {response.status_code} {description or ''}")
        return response.json()


def _notes(metadata: dict[str, str]) -> dict[str, str]:
    """Razorpay allows 15 notes of up to 256 characters each."""
    return {str(k)[:40]: str(v)[:256] for k, v in list(metadata.items())[:MAX_NOTES]}


def _link_session(data: dict[str, Any]) -> CheckoutSession:
    status = data.get("status") or "created"
    paid = status == "paid"
    captured = [p for p in (data.get("payments") or []) if p.get("status") == "captured"]
    return CheckoutSession(
        id=data["id"],
        url=data.get("short_url"),
        status="complete" if paid else "open" if status in ("created", "partially_paid") else "expired",
        payment_status="paid" if paid else "unpaid",
        amount_total=int(data.get("amount_paid") if paid else data.get("amount") or 0),
        currency=(data.get("currency") or "").lower(),
        payment_intent_id=captured[0].get("payment_id") if paid and captured else None,
        metadata={str(k): str(v) for k, v in (data.get("notes") or {}).items()},
    )


class RazorpayPaymentProvider:
    """Razorpay Payment Links (UPI, cards, netbanking on Razorpay's hosted page), test mode."""

    name = "razorpay"
    is_mock = False

    def __init__(self, key_id: str, key_secret: str, webhook_secret: str, *, base: str | None = None,
                 transport: httpx.BaseTransport | None = None):
        if not key_id.startswith("rzp_test_"):
            raise ValueError("Only Razorpay test-mode keys are accepted")
        self._api = _Api(key_id, key_secret, base or get_settings().razorpay_api_base, transport)
        self._webhook_secret = webhook_secret

    def create_checkout_session(self, request: CheckoutRequest) -> CheckoutSession:
        base = get_settings().app_base_url.rstrip("/")
        # Razorpay appends razorpay_payment_link_id (and friends) to the callback. Our return route
        # turns that into the session_id the module's own return page expects.
        callback = f"{base}/payments/razorpay/return?next={quote(request.success_url, safe='')}"
        expire_by = max(request.expires_at or 0, int(time.time()) + MIN_LINK_SECONDS)
        body = {
            "amount": request.amount_paise,
            "currency": request.currency.upper(),
            "accept_partial": False,
            "description": "; ".join(f"{line.name} x{line.quantity}" for line in request.lines)[:2048],
            # Must be unique per link: a fresh link for the same order gets a fresh suffix.
            "reference_id": f"{(request.reference or 'whodoo')[:28]}-{secrets.token_hex(5)}",
            "expire_by": expire_by,
            "notes": _notes(request.metadata),
            "callback_url": callback,
            "callback_method": "get",
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
        }
        return _link_session(self._api.call("POST", "/payment_links", json_body=body))

    def verify_payment(self, session_id: str) -> CheckoutSession | None:
        if not PAYMENT_LINK_ID.match(session_id or ""):
            return None
        data = self._api.call("GET", f"/payment_links/{session_id}", missing_ok=True)
        return _link_session(data) if data else None

    def handle_webhook(self, body: bytes, signature: str) -> WebhookEvent:
        verify_signature(body, signature, self._webhook_secret)
        return translate_webhook(body)

    def refund(self, payment_intent_id: str, amount_paise: int, idempotency_key: str) -> RefundResult:
        # Idempotent on our side: a refund already made under this key is returned, not repeated.
        existing = self._api.call("GET", f"/payments/{payment_intent_id}/refunds") or {}
        for item in existing.get("items") or []:
            if (item.get("notes") or {}).get("idempotency_key") == idempotency_key:
                return _refund_result(item)
        data = self._api.call(
            "POST", f"/payments/{payment_intent_id}/refund",
            json_body={"amount": amount_paise, "speed": "normal", "notes": {"idempotency_key": idempotency_key[:256]}},
        )
        return _refund_result(data)


def _refund_result(data: dict[str, Any]) -> RefundResult:
    status = {"processed": "succeeded", "failed": "failed"}.get(data.get("status") or "", "pending")
    return RefundResult(id=data.get("id"), status=status, failure_reason=data.get("error_description"))


def translate_webhook(body: bytes) -> WebhookEvent:
    """A verified Razorpay event, in the Stripe-shaped form the module handlers already read.

    The id is derived from the entities, so a repeated delivery of the same event has the same
    id and the modules' unique keys turn it away.
    """
    try:
        raw = json.loads(body)
    except ValueError as exc:
        raise InvalidWebhookSignature("Body is not JSON") from exc
    kind = raw.get("event") or ""
    payload = raw.get("payload") or {}

    def entity(name: str) -> dict[str, Any]:
        return (payload.get(name) or {}).get("entity") or {}

    link, payment, refund = entity("payment_link"), entity("payment"), entity("refund")
    if kind == "payment_link.paid":
        return WebhookEvent(
            id=f"rzp:{kind}:{link.get('id')}:{payment.get('id')}", type="checkout.session.completed",
            data={"id": link.get("id"), "payment_status": "paid", "status": "complete",
                  "metadata": link.get("notes") or {}, "amount_total": link.get("amount_paid"),
                  "currency": (link.get("currency") or "").lower(), "payment_intent": payment.get("id")},
        )
    if kind in ("payment_link.expired", "payment_link.cancelled"):
        return WebhookEvent(id=f"rzp:{kind}:{link.get('id')}", type="checkout.session.expired",
                            data={"id": link.get("id"), "metadata": link.get("notes") or {}})
    if kind == "refund.processed":
        return WebhookEvent(
            id=f"rzp:{kind}:{refund.get('id')}", type="charge.refunded",
            data={"payment_intent": refund.get("payment_id"),
                  "amount_refunded": payment.get("amount_refunded", refund.get("amount"))},
        )
    if kind == "refund.failed":
        return WebhookEvent(
            id=f"rzp:{kind}:{refund.get('id')}", type="refund.failed",
            data={"id": refund.get("id"), "payment_intent": refund.get("payment_id"), "status": "failed",
                  "amount": refund.get("amount"), "failure_reason": "refund failed at Razorpay"},
        )
    # Payout events and anything else: handed over untouched, for the payout handler.
    return WebhookEvent(id=f"rzp:{kind}:{secrets.token_hex(8)}", type=f"razorpay.{kind}", data=payload)


# --- Payouts (reimbursements) -----------------------------------------------------------


@dataclass
class VpaCheck:
    status: str                    # verified | invalid | unverified
    registered_name: str | None = None
    reference: str | None = None   # the provider's fund account id
    detail: str | None = None


@dataclass
class PayoutResult:
    id: str | None
    status: str                    # processing | processed | failed | reversed
    failure_reason: str | None = None
    utr: str | None = None


PAYOUT_STATUS = {
    "queued": "processing", "pending": "processing", "scheduled": "processing", "processing": "processing",
    "processed": "processed", "reversed": "reversed",
    "failed": "failed", "rejected": "failed", "cancelled": "failed",
}


class PayoutProvider(Protocol):
    name: str
    is_mock: bool

    def validate_vpa(self, vpa: str, name: str, reference: str) -> VpaCheck: ...

    def create_payout(self, *, vpa: str, name: str, fund_account_id: str | None, amount_paise: int,
                      idempotency_key: str, reference: str, narration: str) -> PayoutResult: ...

    def fetch_payout(self, payout_id: str) -> PayoutResult | None: ...


class RazorpayXPayoutProvider:
    """RazorpayX payouts to a UPI id, test mode. Needs a RazorpayX account number."""

    name = "razorpayx"
    is_mock = False

    def __init__(self, key_id: str, key_secret: str, account_number: str, *, base: str | None = None,
                 transport: httpx.BaseTransport | None = None):
        if not key_id.startswith("rzp_test_"):
            raise ValueError("Only Razorpay test-mode keys are accepted")
        self._api = _Api(key_id, key_secret, base or get_settings().razorpay_api_base, transport)
        self._account = account_number

    def _fund_account(self, vpa: str, name: str, reference: str) -> str:
        contact = self._api.call("POST", "/contacts", json_body={
            "name": name[:50] or "WhoDoo member", "type": "employee", "reference_id": reference[:40]})
        account = self._api.call("POST", "/fund_accounts", json_body={
            "contact_id": contact["id"], "account_type": "vpa", "vpa": {"address": vpa}})
        return account["id"]

    def validate_vpa(self, vpa: str, name: str, reference: str) -> VpaCheck:
        fund_account_id = self._fund_account(vpa, name, reference)
        data = self._api.call("POST", "/fund_accounts/validations", json_body={
            "account_number": self._account, "fund_account": {"id": fund_account_id},
            "amount": 100, "currency": "INR", "notes": {"purpose": "upi_verification"}})
        results = data.get("results") or {}
        if data.get("status") != "completed":
            return VpaCheck("unverified", reference=fund_account_id, detail="validation still in progress")
        if results.get("account_status") == "active":
            return VpaCheck("verified", results.get("registered_name"), fund_account_id)
        return VpaCheck("invalid", reference=fund_account_id, detail="the UPI id is not active")

    def create_payout(self, *, vpa: str, name: str, fund_account_id: str | None, amount_paise: int,
                      idempotency_key: str, reference: str, narration: str) -> PayoutResult:
        fund_account_id = fund_account_id or self._fund_account(vpa, name, reference)
        data = self._api.call(
            "POST", "/payouts",
            headers={"X-Payout-Idempotency": idempotency_key},
            json_body={"account_number": self._account, "fund_account_id": fund_account_id,
                       "amount": amount_paise, "currency": "INR", "mode": "UPI", "purpose": "payout",
                       "queue_if_low_balance": True, "reference_id": reference[:40],
                       "narration": re.sub(r"[^A-Za-z0-9 ]", "", narration)[:30] or "WhoDoo reimbursement"},
        )
        return _payout_result(data)

    def fetch_payout(self, payout_id: str) -> PayoutResult | None:
        data = self._api.call("GET", f"/payouts/{payout_id}", missing_ok=True)
        return _payout_result(data) if data else None


def _payout_result(data: dict[str, Any]) -> PayoutResult:
    reason = (data.get("status_details") or {}).get("description") or data.get("failure_reason")
    return PayoutResult(id=data.get("id"), status=PAYOUT_STATUS.get(data.get("status") or "", "processing"),
                        failure_reason=reason, utr=data.get("utr"))


class MockPayoutProvider:
    """Mock payouts: moves no money. Labelled as a mock everywhere it shows.

    A UPI id whose name part starts with "fail" is treated as invalid, so the failure paths
    can be demonstrated. Tests set `next_status` to play a payout that fails or stays pending.
    """

    name = "mock"
    is_mock = True

    def __init__(self):
        self.payouts: dict[str, PayoutResult] = {}      # idempotency key -> result
        self.next_status = "processed"

    def validate_vpa(self, vpa: str, name: str, reference: str) -> VpaCheck:
        if vpa.split("@")[0].startswith("fail"):
            return VpaCheck("invalid", detail="the mock provider rejects this UPI id")
        return VpaCheck("verified", registered_name=name, reference=f"fa_mock_{secrets.token_hex(6)}")

    def create_payout(self, *, vpa: str, name: str, fund_account_id: str | None, amount_paise: int,
                      idempotency_key: str, reference: str, narration: str) -> PayoutResult:
        if idempotency_key not in self.payouts:
            status = self.next_status
            self.payouts[idempotency_key] = PayoutResult(
                id=f"pout_mock_{secrets.token_hex(8)}", status=status,
                failure_reason="declined by the mock provider" if status == "failed" else None,
                utr=f"MOCK{secrets.token_hex(5).upper()}" if status == "processed" else None,
            )
        return self.payouts[idempotency_key]

    def fetch_payout(self, payout_id: str) -> PayoutResult | None:
        return next((p for p in self.payouts.values() if p.id == payout_id), None)
