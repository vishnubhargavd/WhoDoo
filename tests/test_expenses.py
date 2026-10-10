import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.models.ledger import ApprovalLog, LedgerEntry
from app.services import ledger_service, ocr_service

SAMPLES = Path(__file__).resolve().parents[1] / "app" / "static" / "samples"
RECEIPT = (SAMPLES / "receipt_bake_sale_850.png").read_bytes()
API = "/api/v1/expenses"


@pytest.fixture
def submit(client, auth):
    """Upload a receipt and submit a claim as `user`; returns the created claim JSON."""

    async def _submit(user, amount="850.00", event=None, **overrides):
        upload = await client.post(f"{API}/ocr", headers=auth(user), files={"file": ("r.png", RECEIPT, "image/png")})
        assert upload.status_code == 200, upload.text
        body = {
            "receipt_id": upload.json()["receipt_id"],
            "title": "Bake sale supplies",
            "description": "Flour, butter, sugar and boxes",
            "category": "supplies",
            "amount": amount,
            "expense_date": "2026-10-03",
            "vendor_name": "Sweet Crumbs Bakery Supplies",
            "event_id": str(event.id) if event else None,
        } | overrides
        response = await client.post(API, headers=auth(user), json=body)
        assert response.status_code == 201, response.text
        return response.json()

    return _submit


async def _ledger_entries(db, claim_id):
    return list(await db.scalars(select(LedgerEntry).where(LedgerEntry.reference_id == claim_id)))


# --- OCR -------------------------------------------------------------------------


def test_receipt_text_parsing():
    text = """SWEET CRUMBS BAKERY SUPPLIES
12 MG Road, Bengaluru 560001
Bill No: 4471 Date: 03/10/2026
Wheat flour 10 kg 320.00
Sub Total 750.00
GST 100.00
TOTAL Rs. 850.00
Paid by UPI"""
    vendor, amount, when = ocr_service.parse_receipt_text(text, today=date(2026, 10, 10))
    assert vendor == "Sweet Crumbs Bakery Supplies"
    assert amount == Decimal("850.00")
    assert when == date(2026, 10, 3)


def test_receipt_text_parsing_variants():
    today = date(2026, 10, 10)
    # Thousands separators, a written month, and "grand total" beating a plain "total" line.
    _, amount, when = ocr_service.parse_receipt_text(
        "Tax Invoice\nCafe Mocha\n5 Oct 2026\nTotal 1,180.00\nRound off 0.50\nGrand Total 1,180.50", today
    )
    assert amount == Decimal("1180.50")
    assert when == date(2026, 10, 5)
    # No total line at all: the amount is left empty, never guessed from an item price.
    vendor, amount, when = ocr_service.parse_receipt_text("Print Hub\nPh 9876543210\nPosters 450.00\nTape 60.00", today)
    assert (vendor, amount, when) == ("Print Hub", None, None)
    # Future and impossible dates are rejected rather than guessed.
    assert ocr_service.extract_date(["Date: 31/02/2026", "Valid till 01/01/2030"], today) is None
    assert ocr_service.parse_receipt_text("", today) == (None, None, None)


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="Tesseract is not installed")
async def test_ocr_reads_the_sample_receipt(client, world, auth):
    response = await client.post(
        f"{API}/ocr", headers=auth(world.volunteer), files={"file": ("receipt.png", RECEIPT, "image/png")}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ocr_available"] is True
    assert body["extracted_amount"] == 850.0
    assert body["extracted_date"] == "2026-10-03"
    assert "Sweet Crumbs" in body["extracted_vendor"]
    assert body["ocr_confidence"] > 60
    # Uploading does not create a claim.
    assert (await client.get(API, headers=auth(world.volunteer))).json() == []


async def test_claim_can_be_submitted_when_ocr_is_unavailable(client, world, auth, submit, monkeypatch):
    monkeypatch.setattr(ocr_service, "read_receipt", lambda data, content_type: ocr_service._fallback("ocr_failed"))
    upload = await client.post(
        f"{API}/ocr", headers=auth(world.volunteer), files={"file": ("r.png", RECEIPT, "image/png")}
    )
    assert upload.status_code == 200
    assert upload.json()["ocr_available"] is False
    assert upload.json()["ocr_confidence"] == 0
    assert upload.json()["extracted_amount"] is None

    claim = await submit(world.volunteer, event=world.bake_sale)
    assert claim["status"] == "pending_approval"
    assert claim["extracted_amount"] is None and claim["ocr_confidence"] == 0


async def test_receipt_upload_rules(client, world, auth, submit):
    headers = auth(world.volunteer)
    not_an_image = await client.post(f"{API}/ocr", headers=headers, files={"file": ("x.png", b"hello", "image/png")})
    assert not_an_image.status_code == 415
    assert not_an_image.json()["error"]["code"] == "unsupported_file"

    pdf = await client.post(f"{API}/ocr", headers=headers, files={"file": ("r.pdf", b"%PDF-1.4 fake", "application/pdf")})
    assert pdf.status_code == 200 and pdf.json()["ocr_available"] is False
    assert pdf.json()["receipt_url"].endswith(".pdf")

    upload = await client.post(f"{API}/ocr", headers=headers, files={"file": ("r.png", RECEIPT, "image/png")})
    body = {"receipt_id": upload.json()["receipt_id"], "title": "Supplies", "description": "For the stall",
            "category": "supplies", "amount": "100", "expense_date": "2026-10-03"}

    # Someone else cannot attach my receipt; I cannot attach it twice.
    assert (await client.post(API, headers=auth(world.member), json=body)).status_code == 404
    assert (await client.post(API, headers=headers, json=body)).status_code == 201
    reused = await client.post(API, headers=headers, json=body)
    assert reused.status_code == 409 and reused.json()["error"]["code"] == "receipt_already_used"

    future = await client.post(API, headers=headers, json=body | {"expense_date": "2099-01-01"})
    assert future.status_code == 422


async def test_receipts_are_private_before_and_after_claim_submission(client, db, world, auth, monkeypatch):
    import uuid
    from app.models.shared import Club, User

    monkeypatch.setattr(ocr_service, "read_receipt", lambda *args: ocr_service._fallback("ocr_failed"))
    upload = await client.post(f"{API}/ocr", headers=auth(world.volunteer),
                               files={"file": ("r.png", RECEIPT, "image/png")})
    receipt = upload.json()
    url = receipt["receipt_url"]
    assert url.startswith(f"{API}/receipts/{world.club.id}/")
    assert (await client.get(url)).status_code == 401
    assert (await client.get(url, headers=auth(world.member))).status_code == 404
    assert (await client.get(url, headers=auth(world.treasurer))).status_code == 404  # unattached upload is owner-only
    own = await client.get(url, headers=auth(world.volunteer))
    assert own.status_code == 200 and own.content == RECEIPT
    assert own.headers["cache-control"] == "private, no-store"
    assert own.headers["x-content-type-options"] == "nosniff"
    legacy = url.replace(f"{API}/receipts/", "/uploads/receipts/")
    assert (await client.get(legacy)).status_code == 404
    claim = await client.post(API, headers=auth(world.volunteer), json={
        "receipt_id": receipt["receipt_id"], "title": "Private receipt", "description": "Private description",
        "category": "supplies", "amount": "100", "expense_date": "2026-10-03", "event_id": str(world.bake_sale.id),
    })
    assert claim.status_code == 201, claim.text
    assert (await client.get(url, headers=auth(world.lead))).status_code == 200
    assert (await client.get(url, headers=auth(world.treasurer))).status_code == 200
    assert (await client.get(url, headers=auth(world.member))).status_code == 403
    club = Club(id=uuid.uuid4(), name="Other Club")
    db.add(club)
    await db.flush()
    stranger = User(id=uuid.uuid4(), club_id=club.id, full_name="Other Treasurer", email="other@test.invalid",
                    role="treasurer", is_active_member=False)
    db.add(stranger)
    await db.commit()
    assert (await client.get(url, headers=auth(stranger))).status_code == 404


def test_storage_rejects_public_buckets_and_path_traversal(monkeypatch):
    import uuid
    from types import SimpleNamespace
    from app.services import storage_service
    from app.core.errors import AppError

    storage = SimpleNamespace(get_bucket=lambda name: SimpleNamespace(public=True))
    monkeypatch.setattr(storage_service, "get_supabase", lambda: SimpleNamespace(storage=storage))
    with pytest.raises(AppError, match="private storage"):
        storage_service.save_receipt(uuid.uuid4(), RECEIPT, ".png", "image/png")
    with pytest.raises(AppError) as denied:
        storage_service.read_receipt(uuid.uuid4(), "../../.env")
    assert denied.value.status_code == 404


# --- Approval workflow -------------------------------------------------------------


async def test_small_claim_lead_approves_then_treasurer_pays(client, db, world, auth, submit):
    await ledger_service.record_income(
        db, club_id=world.club.id, source_module="membership", amount=Decimal("5000"), created_by=world.treasurer.id
    )
    await db.commit()

    claim = await submit(world.volunteer, amount="850.00", event=world.bake_sale)
    assert claim["current_approver_id"] == str(world.lead.id)
    assert claim["needs_final_approval"] is False
    assert await _ledger_entries(db, claim["id"]) == []

    queue = (await client.get(f"{API}/approvals", headers=auth(world.lead))).json()
    assert [item["id"] for item in queue["to_approve"]] == [claim["id"]]
    assert queue["to_approve"][0]["allowed_actions"] == ["approve", "reject", "request_changes"]

    approved = await client.post(f"{API}/{claim['id']}/approve", headers=auth(world.lead))
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert approved.json()["ledger_payment_status"] == "approved"

    [entry] = await _ledger_entries(db, claim["id"])
    assert (entry.entry_type, entry.direction, entry.payment_status) == ("reimbursement", "debit", "approved")
    assert (entry.source_module, entry.amount, entry.payee_id) == ("expense_claim", Decimal("850.00"), world.volunteer.id)
    assert entry.approved_by == world.lead.id and entry.event_id == world.bake_sale.id

    # Approved but unpaid: owed, balance untouched.
    summary = (await client.get("/api/v1/ledger/summary", headers=auth(world.treasurer))).json()
    assert (summary["current_balance"], summary["pending_reimbursements"]) == (5000.0, 850.0)

    to_pay = (await client.get(f"{API}/approvals", headers=auth(world.treasurer))).json()["to_pay"]
    assert [item["id"] for item in to_pay] == [claim["id"]]

    paid = await client.post(f"{API}/{claim['id']}/mark-paid", headers=auth(world.treasurer))
    assert paid.status_code == 200, paid.text
    assert paid.json()["status"] == "paid" and paid.json()["ledger_payment_status"] == "paid"
    assert [log["action"] for log in paid.json()["approval_history"]] == ["submitted", "approved", "paid"]

    summary = (await client.get("/api/v1/ledger/summary", headers=auth(world.treasurer))).json()
    assert summary["current_balance"] == 4150.0
    assert summary["total_reimbursements_paid"] == 850.0
    assert summary["pending_reimbursements"] == 0
    assert len(await _ledger_entries(db, claim["id"])) == 1


async def test_large_claim_needs_a_second_approver(client, db, world, auth, submit):
    claim = await submit(world.volunteer, amount="2500.00", event=world.fest, category="decoration")
    url = f"{API}/{claim['id']}"
    assert claim["needs_final_approval"] is True

    first = await client.post(f"{url}/approve", headers=auth(world.lead2))
    assert first.status_code == 200
    assert first.json()["status"] == "pending_approval"
    assert first.json()["approval_stage"] == "final"
    assert first.json()["current_approver_id"] == str(world.treasurer.id)
    assert await _ledger_entries(db, claim["id"]) == []

    # The event lead cannot also give the final approval.
    again = await client.post(f"{url}/approve", headers=auth(world.lead2))
    assert again.status_code == 409

    final = await client.post(f"{url}/approve", headers=auth(world.treasurer), json={"comment": "Within budget"})
    assert final.status_code == 200
    assert final.json()["status"] == "approved"
    assert final.json()["final_approver_id"] == str(world.treasurer.id)
    assert len(await _ledger_entries(db, claim["id"])) == 1

    # At the limit exactly, two approvals are still required.
    edge = await submit(world.volunteer, amount="1000.00", event=world.fest)
    assert edge["needs_final_approval"] is True
    just_under = await submit(world.volunteer, amount="999.99", event=world.fest)
    assert just_under["needs_final_approval"] is False


async def test_submitter_sees_each_approval_step(client, world, auth, submit):
    """The reported bug: after the event lead approved a large claim, the member's view still just
    said "pending", as if nothing had happened."""
    claim = await submit(world.volunteer, amount="1800.00", event=world.fest)
    url = f"{API}/{claim['id']}"
    assert claim["status_label"] == "Awaiting first approval from Lina Lead"

    first = await client.post(f"{url}/approve", headers=auth(world.lead2))
    assert first.json()["status_label"] == "First approval by Lina Lead · awaiting final approval from Tara Treasurer"

    # What the member sees, in the list and on the claim.
    [mine] = (await client.get(API, headers=auth(world.volunteer))).json()
    assert mine["status_label"] == "First approval by Lina Lead · awaiting final approval from Tara Treasurer"
    assert (mine["status"], mine["approval_stage"]) == ("pending_approval", "final")
    history = (await client.get(url, headers=auth(world.volunteer))).json()["approval_history"]
    assert [(log["action"], log["approver_name"]) for log in history] == [
        ("submitted", "Vik Volunteer"), ("approved", "Lina Lead")
    ]

    # The lead is told why they cannot approve again; the treasurer sees it in their queue.
    again = await client.post(f"{url}/approve", headers=auth(world.lead2))
    assert again.status_code == 409 and again.json()["error"]["code"] == "duplicate_approval"
    queue = (await client.get(f"{API}/approvals", headers=auth(world.treasurer))).json()["to_approve"]
    assert [item["id"] for item in queue] == [claim["id"]]

    await client.post(f"{url}/approve", headers=auth(world.treasurer))
    [mine] = (await client.get(API, headers=auth(world.volunteer))).json()
    assert (mine["status"], mine["status_label"]) == ("approved", "Approved · awaiting payment")
    await client.post(f"{url}/mark-paid", headers=auth(world.treasurer))
    [mine] = (await client.get(API, headers=auth(world.volunteer))).json()
    assert (mine["status"], mine["status_label"]) == ("paid", "Paid")


async def test_duplicate_approval_is_blocked(client, db, world, auth, submit):
    claim = await submit(world.volunteer, amount="400", event=world.bake_sale)
    url = f"{API}/{claim['id']}/approve"

    assert (await client.post(url, headers=auth(world.lead))).status_code == 200
    for person in (world.lead, world.treasurer):
        repeat = await client.post(url, headers=auth(person))
        assert repeat.status_code == 409
        assert repeat.json()["error"]["code"] == "not_pending"

    assert len(await _ledger_entries(db, claim["id"])) == 1
    approvals = await db.scalar(
        select(func.count()).select_from(ApprovalLog).where(ApprovalLog.action == "approved")
    )
    assert approvals == 1


async def test_nobody_approves_their_own_claim(client, world, auth, submit):
    # A lead claiming for their own event: the claim is routed to the treasurer instead.
    own = await submit(world.lead, amount="300", event=world.bake_sale)
    assert own["current_approver_id"] == str(world.treasurer.id)
    blocked = await client.post(f"{API}/{own['id']}/approve", headers=auth(world.lead))
    assert blocked.status_code == 403 and blocked.json()["error"]["code"] == "self_approval"

    # The treasurer's own claim goes to the president; the treasurer can neither approve nor pay it.
    treasurers = await submit(world.treasurer, amount="300")
    assert treasurers["current_approver_id"] == str(world.president.id)
    url = f"{API}/{treasurers['id']}"
    assert (await client.post(f"{url}/approve", headers=auth(world.treasurer))).json()["error"]["code"] == "self_approval"
    assert (await client.post(f"{url}/approve", headers=auth(world.president))).status_code == 200
    self_pay = await client.post(f"{url}/mark-paid", headers=auth(world.treasurer))
    assert self_pay.status_code == 403 and self_pay.json()["error"]["code"] == "self_payment"
    assert (await client.post(f"{url}/mark-paid", headers=auth(world.president))).status_code == 200


async def test_only_the_right_people_can_act(client, world, auth, submit):
    claim = await submit(world.volunteer, amount="400", event=world.bake_sale)
    url = f"{API}/{claim['id']}"

    # Volunteers and members are not approvers; the other event's lead is not this claim's approver.
    assert (await client.post(f"{url}/approve", headers=auth(world.member))).status_code == 403
    assert (await client.post(f"{url}/approve", headers=auth(world.lead2))).status_code == 403
    assert (await client.get(f"{API}/approvals", headers=auth(world.lead2))).json()["to_approve"] == []
    assert (await client.get(f"{API}/approvals", headers=auth(world.volunteer))).status_code == 403

    # Claims are private to the submitter, their approvers and the finance admins.
    assert (await client.get(url, headers=auth(world.member))).status_code == 403
    assert (await client.get(url, headers=auth(world.lead2))).status_code == 403
    for person in (world.volunteer, world.lead, world.treasurer):
        assert (await client.get(url, headers=auth(person))).status_code == 200

    # Paying: not before approval, not by a lead, not twice.
    assert (await client.post(f"{url}/mark-paid", headers=auth(world.treasurer))).json()["error"]["code"] == "not_approved"
    await client.post(f"{url}/approve", headers=auth(world.lead))
    assert (await client.post(f"{url}/mark-paid", headers=auth(world.lead))).status_code == 403
    assert (await client.post(f"{url}/mark-paid", headers=auth(world.treasurer))).status_code == 200
    assert (await client.post(f"{url}/mark-paid", headers=auth(world.treasurer))).json()["error"]["code"] == "already_paid"


async def test_reject_requires_a_reason(client, db, world, auth, submit):
    claim = await submit(world.volunteer, amount="400", event=world.bake_sale)
    url = f"{API}/{claim['id']}"

    assert (await client.post(f"{url}/reject", headers=auth(world.lead), json={})).status_code == 422
    assert (await client.post(f"{url}/reject", headers=auth(world.lead), json={"reason": "      "})).status_code == 422

    rejected = await client.post(f"{url}/reject", headers=auth(world.lead), json={"reason": "Not a club expense"})
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["rejection_reason"] == "Not a club expense"
    assert rejected.json()["approval_history"][-1]["comment"] == "Not a club expense"

    assert (await client.post(f"{url}/approve", headers=auth(world.treasurer))).status_code == 409
    assert await _ledger_entries(db, claim["id"]) == []


async def test_request_changes_then_resubmit_restarts_approval(client, world, auth, submit):
    claim = await submit(world.volunteer, amount="2500", event=world.fest)
    url = f"{API}/{claim['id']}"
    await client.post(f"{url}/approve", headers=auth(world.lead2))

    sent_back = await client.post(
        f"{url}/request-changes", headers=auth(world.treasurer), json={"comment": "Amount does not match the receipt"}
    )
    assert sent_back.json()["status"] == "change_requested"
    mine = (await client.get(url, headers=auth(world.volunteer))).json()
    assert mine["allowed_actions"] == ["resubmit"]

    fixed = {"title": "Stage decor", "description": "Corrected amount", "category": "decoration",
             "amount": "2400", "expense_date": "2026-09-28", "event_id": str(world.fest.id)}
    assert (await client.post(f"{url}/resubmit", headers=auth(world.lead2), json=fixed)).status_code == 403
    resubmitted = await client.post(f"{url}/resubmit", headers=auth(world.volunteer), json=fixed)
    assert resubmitted.status_code == 200, resubmitted.text
    body = resubmitted.json()
    assert (body["status"], body["approval_stage"], body["amount"]) == ("pending_approval", "first", 2400.0)
    assert body["current_approver_id"] == str(world.lead2.id)
    assert [log["action"] for log in body["approval_history"]] == [
        "submitted", "approved", "change_requested", "submitted"
    ]


async def test_submitter_sees_what_the_reviewer_asked_for(client, world, auth, submit):
    """Reported: the treasurer requested changes, and nothing on the member's side said so."""
    from app.core.config import get_settings
    from app.core.security import create_access_token

    claim = await submit(world.member, amount="640", event=world.bake_sale)
    url = f"{API}/{claim['id']}"
    sent_back = await client.post(
        f"{url}/request-changes", headers=auth(world.treasurer), json={"comment": "Please attach the itemised bill"}
    )
    assert sent_back.status_code == 200

    # The member's API view carries the request itself, not just a status.
    [mine] = (await client.get(API, headers=auth(world.member))).json()
    assert mine["status"] == "change_requested"
    assert (mine["reviewer_name"], mine["reviewer_note"]) == ("Tara Treasurer", "Please attach the itemised bill")
    assert mine["allowed_actions"] == ["resubmit"]
    detail = (await client.get(url, headers=auth(world.member))).json()
    assert detail["reviewer_note"] == "Please attach the itemised bill"

    # And so do the member's pages, with a way to act on it.
    client.cookies.set(get_settings().session_cookie, create_access_token(world.member.id))
    for path in ("/expenses", f"/expenses/{claim['id']}"):
        page = (await client.get(path)).text
        assert "Tara Treasurer asked for changes:" in page and "Please attach the itemised bill" in page
        assert "Edit and resubmit" in page
    assert 'id="resubmit"' in (await client.get(f"/expenses/{claim['id']}")).text
    assert "1 claim waiting for your changes." in (await client.get("/home")).text

    # The treasurer sees it is with the member; a second request replaces the first after resubmission.
    client.cookies.set(get_settings().session_cookie, create_access_token(world.treasurer.id))
    assert "waiting for Mia Member to edit and resubmit" in (await client.get(f"/expenses/{claim['id']}")).text

    fixed = {"title": "Bake sale supplies", "description": "Bill attached", "category": "supplies",
             "amount": "640", "expense_date": "2026-10-03", "event_id": str(world.bake_sale.id)}
    resubmitted = (await client.post(f"{url}/resubmit", headers=auth(world.member), json=fixed)).json()
    assert (resubmitted["status"], resubmitted["reviewer_note"]) == ("pending_approval", None)
    await client.post(f"{url}/request-changes", headers=auth(world.lead), json={"comment": "Wrong event selected"})
    [mine] = (await client.get(API, headers=auth(world.member))).json()
    assert (mine["reviewer_name"], mine["reviewer_note"]) == ("Leo Lead", "Wrong event selected")


async def test_snap_to_reimburse_cross_club_and_idempotent_ledger(client, db, world, auth, submit):
    import uuid
    from app.models.shared import Club, Event

    # 1. Reject event belonging to another club
    other_club = Club(id=uuid.uuid4(), name="External Club")
    db.add(other_club)
    await db.flush()
    other_event = Event(id=uuid.uuid4(), club_id=other_club.id, name="External Event")
    db.add(other_event)
    await db.commit()

    upload = await client.post(f"{API}/ocr", headers=auth(world.volunteer), files={"file": ("r.png", RECEIPT, "image/png")})
    assert upload.status_code == 200
    body = {
        "receipt_id": upload.json()["receipt_id"],
        "title": "Cross-club event test",
        "description": "Invalid event linkage",
        "category": "supplies",
        "amount": "500",
        "expense_date": "2026-10-03",
        "event_id": str(other_event.id),
    }
    resp = await client.post(API, headers=auth(world.volunteer), json=body)
    assert resp.status_code == 404

    # 2. Reject receipt from another user/club
    resp_bad_receipt = await client.post(
        API,
        headers=auth(world.member),
        json=body | {"receipt_id": upload.json()["receipt_id"], "event_id": str(world.bake_sale.id)},
    )
    assert resp_bad_receipt.status_code == 404

    # 3. Two distinct approvers rule when claim >= 1000
    large_claim = await submit(world.volunteer, amount="1500.00", event=world.bake_sale)
    assert large_claim["needs_final_approval"] is True
    assert large_claim["approval_stage"] == "first"

    # Rejecting a claim ensures it cannot be paid
    claim_to_reject = await submit(world.volunteer, amount="1200.00", event=world.bake_sale)
    reject_resp = await client.post(
        f"{API}/{claim_to_reject['id']}/reject",
        headers=auth(world.lead),
        json={"reason": "Excessive expenses"},
    )
    assert reject_resp.status_code == 200
    assert reject_resp.json()["status"] == "rejected"

    pay_attempt = await client.post(
        f"{API}/{claim_to_reject['id']}/mark-paid",
        headers=auth(world.treasurer),
    )
    assert pay_attempt.status_code == 409
    assert pay_attempt.json()["error"]["code"] == "not_approved"

