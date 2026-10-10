"""Tests for Task 1: Razorpay Test Mode, Secure UPI Profiles, and Reimbursement Payouts."""

import hashlib
import hmac
import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.core.config import get_settings
from app.models.expense import ExpenseClaim
from app.models.payment_profile import UserPaymentProfile
from app.models.shared import Club, User
from app.services.razorpay_service import compute_razorpay_signature


async def test_upi_profile_crud_and_masking(client, db, world, auth):
    # 1. Initially no UPI configured
    res = await client.get("/api/v1/upi/profile", headers=auth(world.member))
    assert res.status_code == 200
    data = res.json()
    assert data["upi_id_masked"] is None
    assert data["verification_status"] == "not_configured"

    # 2. Add valid UPI ID
    update_res = await client.put(
        "/api/v1/upi/profile",
        headers=auth(world.member),
        json={
            "upi_id": "priya.sharma@okaxis",
            "preferred_payout_method": "upi",
        },
    )
    assert update_res.status_code == 200
    upd_data = update_res.json()
    assert upd_data["is_syntax_valid"] is True
    # Does not falsely claim bank verification
    assert upd_data["verification_status"] == "unverified"
    # Masked output
    assert "@okaxis" in upd_data["upi_id_masked"]
    assert "priya.sharma" not in upd_data["upi_id_masked"]
    assert "*" in upd_data["upi_id_masked"]

    # 3. Reject invalid syntax
    invalid_res = await client.put(
        "/api/v1/upi/profile",
        headers=auth(world.member),
        json={"upi_id": "not-a-valid-vpa"},
    )
    assert invalid_res.status_code == 422 or invalid_res.status_code == 400

    # 4. Remove UPI profile
    del_res = await client.delete("/api/v1/upi/profile", headers=auth(world.member))
    assert del_res.status_code == 200
    assert del_res.json()["upi_id_masked"] is None


async def test_upi_profile_isolation(client, db, world, auth):
    # Member adds UPI
    await client.put(
        "/api/v1/upi/profile",
        headers=auth(world.member),
        json={"upi_id": "member@okhdfcbank"},
    )
    # Volunteer adds different UPI
    await client.put(
        "/api/v1/upi/profile",
        headers=auth(world.volunteer),
        json={"upi_id": "volunteer@icici"},
    )

    mem_view = (await client.get("/api/v1/upi/profile", headers=auth(world.member))).json()
    vol_view = (await client.get("/api/v1/upi/profile", headers=auth(world.volunteer))).json()

    assert "@okhdfcbank" in mem_view["upi_id_masked"]
    assert "@icici" in vol_view["upi_id_masked"]
    assert mem_view["user_id"] != vol_view["user_id"]


async def test_razorpay_order_and_signature_verification(client, db, world, auth):
    secret = "rzp_test_secret"
    # Create order
    ref_id = uuid.uuid4()
    order_res = await client.post(
        "/api/v1/payments/razorpay/order",
        headers=auth(world.member),
        json={
            "reference_type": "ticket_booking",
            "reference_id": str(ref_id),
            "amount_paise": 150000,
            "currency": "INR",
        },
    )
    assert order_res.status_code == 201
    order_data = order_res.json()
    assert order_data["amount_paise"] == 150000
    rzp_order_id = order_data["rzp_order_id"]
    assert rzp_order_id.startswith("order_test_")

    # Tampered signature fails
    fail_res = await client.post(
        "/api/v1/payments/razorpay/verify",
        headers=auth(world.member),
        json={
            "rzp_order_id": rzp_order_id,
            "rzp_payment_id": "pay_test_12345",
            "rzp_signature": "invalid_forged_signature",
        },
    )
    assert fail_res.status_code == 400
    assert "invalid_signature" in fail_res.json()["error"]["code"]

    # Valid cryptographic signature succeeds
    payment_id = "pay_test_real123"
    valid_sig = compute_razorpay_signature(rzp_order_id, payment_id, secret)

    verify_res = await client.post(
        "/api/v1/payments/razorpay/verify",
        headers=auth(world.member),
        json={
            "rzp_order_id": rzp_order_id,
            "rzp_payment_id": payment_id,
            "rzp_signature": valid_sig,
        },
    )
    assert verify_res.status_code == 200
    assert verify_res.json()["status"] == "success"

    # Idempotent retry
    retry_res = await client.post(
        "/api/v1/payments/razorpay/verify",
        headers=auth(world.member),
        json={
            "rzp_order_id": rzp_order_id,
            "rzp_payment_id": payment_id,
            "rzp_signature": valid_sig,
        },
    )
    assert retry_res.status_code == 200


async def test_reimbursement_payout_via_upi(client, db, world, auth):
    # Member adds UPI profile
    await client.put(
        "/api/v1/upi/profile",
        headers=auth(world.member),
        json={"upi_id": "mia.member@okaxis"},
    )

    # Create approved expense claim
    claim = ExpenseClaim(
        id=uuid.uuid4(),
        club_id=world.president.club_id,
        submitted_by=world.member.id,
        title="Event supplies",
        amount=Decimal("1500.00"),
        expense_date=date(2026, 10, 1),
        receipt_url="https://receipts.example.com/banner.pdf",
        category="supplies",
        description="Event banners and stage craft supplies",
        status="approved",
    )
    db.add(claim)
    await db.commit()

    # Officer initiates reimbursement payout
    payout_res = await client.post(
        f"/api/v1/expenses/{claim.id}/payout",
        headers=auth(world.president),
        json={"claim_id": str(claim.id), "mode": "upi"},
    )
    assert payout_res.status_code == 201
    payout_data = payout_res.json()
    assert payout_data["amount_paise"] == 150000
    assert payout_data["mode"] == "upi"
    # When RazorpayX is unconfigured, does not falsely claim PAID
    assert payout_data["status"] in ("pending", "processing")
    assert payout_data["provider"] in ("manual", "razorpayx")

    # Ordinary member cannot initiate payouts
    unauth_payout = await client.post(
        f"/api/v1/expenses/{claim.id}/payout",
        headers=auth(world.member),
        json={"claim_id": str(claim.id), "mode": "upi"},
    )
    assert unauth_payout.status_code == 403
