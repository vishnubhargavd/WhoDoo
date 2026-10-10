"""The reimbursement approval policy.

Below ₹1,000: one authorised approval. At ₹1,000 or more: two approvals from two
different people, the second from a treasurer or president. Nobody approves their own claim.
"""

import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.security import create_access_token
from app.models.expense import ExpenseClaim
from app.models.ledger import ApprovalLog, AuditLog, LedgerEntry
from app.models.shared import Club, User
from tests.test_expenses import API, RECEIPT


@pytest.fixture
def submit(client, auth):
    async def _submit(user, amount, event=None):
        upload = await client.post(f"{API}/ocr", headers=auth(user), files={"file": ("r.png", RECEIPT, "image/png")})
        response = await client.post(API, headers=auth(user), json={
            "receipt_id": upload.json()["receipt_id"], "title": "Equipment hire", "description": "For the event",
            "category": "other", "amount": amount, "expense_date": "2026-10-03",
            "event_id": str(event.id) if event else None,
        })
        assert response.status_code == 201, response.text
        return response.json()

    return _submit


@pytest.fixture
def act(client, auth):
    """POST an action on a claim as a user; returns the response."""

    async def _act(user, claim, action="approve", **body):
        return await client.post(f"{API}/{claim['id']}/{action}", headers=auth(user), json=body or None)

    return _act


async def _row(db, claim) -> ExpenseClaim:
    return await db.scalar(
        select(ExpenseClaim).where(ExpenseClaim.id == claim["id"]).execution_options(populate_existing=True)
    )


async def _entries(db, claim) -> list[LedgerEntry]:
    return list(await db.scalars(select(LedgerEntry).where(LedgerEntry.reference_id == claim["id"])))


async def _approvals(db, claim) -> list[uuid.UUID]:
    logs = await db.scalars(
        select(ApprovalLog)
        .where(ApprovalLog.expense_claim_id == claim["id"], ApprovalLog.action == "approved")
        .order_by(ApprovalLog.created_at)
    )
    return [log.approver_id for log in logs]


# --- 1, 2: the threshold ------------------------------------------------------------------


async def test_just_under_the_limit_needs_one_approval(db, world, submit, act):
    claim = await submit(world.volunteer, "999.99", world.bake_sale)
    approved = await act(world.lead, claim)
    assert approved.json()["status"] == "approved"
    assert await _approvals(db, claim) == [world.lead.id]
    assert len(await _entries(db, claim)) == 1


async def test_exactly_the_limit_needs_two_distinct_approvals(db, world, submit, act):
    claim = await submit(world.volunteer, "1000.00", world.bake_sale)
    first = await act(world.lead, claim)
    assert (first.json()["status"], first.json()["approval_stage"]) == ("pending_approval", "final")
    assert await _entries(db, claim) == []  # no liability in the ledger after only one approval

    final = await act(world.treasurer, claim)
    assert final.json()["status"] == "approved"
    assert await _approvals(db, claim) == [world.lead.id, world.treasurer.id]
    [entry] = await _entries(db, claim)
    assert (entry.amount, entry.payment_status, entry.approved_by) == (Decimal("1000.00"), "approved", world.treasurer.id)


# --- 3, 4: both valid routes ----------------------------------------------------------------


async def test_event_lead_first_then_treasurer(db, world, submit, act):
    claim = await submit(world.volunteer, "1800", world.fest)
    first = (await act(world.lead2, claim)).json()
    assert first["status_label"] == "First approval by Lina Lead · awaiting final approval from Tara Treasurer"
    assert (first["first_approved_by"], first["final_approver_id"]) == (str(world.lead2.id), None)

    final = (await act(world.treasurer, claim)).json()
    assert (final["status"], final["first_approved_by_name"], final["final_approver_name"]) == (
        "approved", "Lina Lead", "Tara Treasurer")
    row = await _row(db, claim)
    assert row.first_approved_by != row.final_approver_id


async def test_treasurer_first_then_president(db, world, submit, act):
    claim = await submit(world.volunteer, "1800", world.fest)

    first = (await act(world.treasurer, claim)).json()
    # A treasurer approving first has NOT also given the final approval.
    assert (first["status"], first["approval_stage"]) == ("pending_approval", "final")
    assert first["status_label"] == "First approval by Tara Treasurer · awaiting final approval from Priya President"
    assert await _entries(db, claim) == []

    final = (await act(world.president, claim)).json()
    assert final["status"] == "approved"
    assert await _approvals(db, claim) == [world.treasurer.id, world.president.id]


# --- 5: the bypass this change closes ----------------------------------------------------------


@pytest.mark.parametrize("who", ["treasurer", "president"])
async def test_same_person_cannot_give_both_approvals(db, world, submit, act, who):
    approver = getattr(world, who)
    claim = await submit(world.volunteer, "1800", world.fest)
    assert (await act(approver, claim)).status_code == 200

    for _ in range(3):  # repeating the request changes nothing
        again = await act(approver, claim)
        assert again.status_code == 409
        assert again.json()["error"]["code"] == "duplicate_approval"

    row = await _row(db, claim)
    assert (row.status, row.approval_stage, row.final_approver_id) == ("pending_approval", "final", None)
    assert await _approvals(db, claim) == [approver.id]
    assert await _entries(db, claim) == []


async def test_first_approver_sees_why_they_cannot_approve_again(client, world, auth, submit, act):
    claim = await submit(world.volunteer, "1800", world.fest)
    await act(world.treasurer, claim)

    mine = (await client.get(f"{API}/{claim['id']}", headers=auth(world.treasurer))).json()
    assert "approve" not in mine["allowed_actions"]
    assert mine["allowed_actions"] == ["reject", "request_changes"]  # they can still stop it
    assert "different treasurer or president" in mine["approval_blocked_reason"]

    queue = (await client.get(f"{API}/approvals", headers=auth(world.treasurer))).json()
    assert queue["to_approve"] == []
    assert [item["id"] for item in queue["awaiting_others"]] == [claim["id"]]

    theirs = (await client.get(f"{API}/{claim['id']}", headers=auth(world.president))).json()
    assert theirs["allowed_actions"] == ["approve", "reject", "request_changes"]
    assert theirs["approval_blocked_reason"] is None


async def test_approver_ids_cannot_be_supplied_by_the_client(db, world, submit, act):
    claim = await submit(world.volunteer, "1800", world.fest)
    forged = await act(
        world.treasurer, claim, comment="ok", first_approved_by=str(world.lead2.id),
        final_approver_id=str(world.president.id), approval_stage="final", status="approved",
    )
    assert forged.status_code == 200
    row = await _row(db, claim)
    assert (row.status, row.approval_stage) == ("pending_approval", "final")
    assert (row.first_approved_by, row.final_approver_id) == (world.treasurer.id, None)


# --- 6 to 9: who may approve at all ---------------------------------------------------------------


async def test_claimant_cannot_approve_at_either_stage(db, world, submit, act):
    # The treasurer's own large claim for an event: the lead approves first, the president finally.
    claim = await submit(world.treasurer, "1800", world.fest)
    blocked = await act(world.treasurer, claim)
    assert blocked.status_code == 403 and blocked.json()["error"]["code"] == "self_approval"

    assert (await act(world.lead2, claim)).json()["approval_stage"] == "final"
    blocked = await act(world.treasurer, claim)  # a treasurer, at the final stage, but it is their claim
    assert blocked.status_code == 403 and blocked.json()["error"]["code"] == "self_approval"
    assert (await _row(db, claim)).status == "pending_approval"
    assert await _approvals(db, claim) == [world.lead2.id]

    assert (await act(world.president, claim)).json()["status"] == "approved"
    paying_self = await act(world.treasurer, claim, "mark-paid")
    assert paying_self.status_code == 403 and paying_self.json()["error"]["code"] == "self_payment"


async def test_claim_stays_put_when_no_second_approver_exists(db, world, submit, act):
    # The president claims; the treasurer is the only other finance admin, so no one could give
    # the final approval after them. The first approval is refused rather than left half-done.
    claim = await submit(world.president, "1800")
    stuck = await act(world.treasurer, claim)
    assert stuck.status_code == 409 and stuck.json()["error"]["code"] == "no_final_approver"
    row = await _row(db, claim)
    assert (row.status, row.approval_stage, row.first_approved_by) == ("pending_approval", "first", None)
    assert await _approvals(db, claim) == []


async def test_unauthorised_members_cannot_approve(db, world, submit, act):
    claim = await submit(world.volunteer, "1800", world.fest)
    for person in (world.member, world.outsider):
        assert (await act(person, claim)).status_code == 403
    await act(world.lead2, claim)
    for person in (world.member, world.outsider, world.lead):  # nor at the final stage
        assert (await act(person, claim)).status_code == 403
    assert await _approvals(db, claim) == [world.lead2.id]


async def test_approver_from_another_club_cannot_approve(client, db, world, submit):
    other_club = Club(id=uuid.uuid4(), name="Other Club")
    db.add(other_club)
    await db.flush()
    outsiders = [
        User(id=uuid.uuid4(), club_id=other_club.id, full_name=f"Other {role}", email=f"{role}@other.example",
             role=role, is_active_member=True)
        for role in ("treasurer", "president")
    ]
    db.add_all(outsiders)
    await db.commit()

    claim = await submit(world.volunteer, "1800", world.fest)
    for person in outsiders:
        headers = {"Authorization": f"Bearer {create_access_token(person.id)}"}
        for action, body in (("approve", None), ("reject", {"reason": "not ours"}), ("mark-paid", None)):
            response = await client.post(f"{API}/{claim['id']}/{action}", headers=headers, json=body)
            assert response.status_code == 404, (person.role, action)  # another club's claim does not exist for them
        assert (await client.get(f"{API}/{claim['id']}", headers=headers)).status_code == 404
        assert (await client.get(f"{API}/approvals", headers=headers)).json()["to_approve"] == []
    assert await _approvals(db, claim) == []


async def test_event_lead_cannot_approve_an_unrelated_event(db, world, submit, act):
    claim = await submit(world.volunteer, "1800", world.fest)  # Tech Fest is led by lead2
    wrong = await act(world.lead, claim)                        # lead runs the Bake Sale
    assert wrong.status_code == 403
    assert (await act(world.lead, claim, "reject", reason="not mine")).status_code == 403
    assert await _approvals(db, claim) == []


# --- 10, 11: payment ----------------------------------------------------------------------------------


async def test_payment_only_after_both_approvals(client, db, world, auth, submit, act):
    claim = await submit(world.volunteer, "1800", world.fest)
    for payer in (world.treasurer, world.president):
        early = await act(payer, claim, "mark-paid")
        assert early.status_code == 409 and early.json()["error"]["code"] == "not_approved"

    await act(world.lead2, claim)
    after_first = await act(world.treasurer, claim, "mark-paid")
    assert after_first.status_code == 409 and after_first.json()["error"]["code"] == "not_approved"
    assert await _entries(db, claim) == []

    await act(world.treasurer, claim)
    paid = await act(world.treasurer, claim, "mark-paid")
    assert paid.status_code == 200 and paid.json()["status"] == "paid"

    # The expense is in the ledger exactly once, for the claimed amount, and is now paid.
    [entry] = await _entries(db, claim)
    assert (entry.amount, entry.entry_type, entry.payment_status) == (Decimal("1800.00"), "reimbursement", "paid")
    summary = (await client.get("/api/v1/ledger/summary", headers=auth(world.treasurer))).json()
    assert summary["total_reimbursements_paid"] == 1800.0
    assert (await act(world.treasurer, claim, "mark-paid")).status_code == 409
    assert len(await _entries(db, claim)) == 1


# --- 12, 13: repeats and races --------------------------------------------------------------------------


async def test_repeated_requests_add_no_approvals(db, world, submit, act):
    claim = await submit(world.volunteer, "1800", world.fest)
    codes = [(await act(world.lead2, claim)).status_code for _ in range(4)]
    assert codes == [200, 409, 409, 409]
    assert await _approvals(db, claim) == [world.lead2.id]

    codes = [(await act(world.treasurer, claim)).status_code for _ in range(4)]
    assert codes == [200, 409, 409, 409]
    assert await _approvals(db, claim) == [world.lead2.id, world.treasurer.id]
    assert len(await _entries(db, claim)) == 1


async def test_simultaneous_requests_from_one_person_count_once(db, world, submit, act):
    claim = await submit(world.volunteer, "1800", world.fest)
    results = await asyncio.gather(*(act(world.treasurer, claim) for _ in range(6)))
    assert sorted(response.status_code for response in results) == [200, 409, 409, 409, 409, 409]

    row = await _row(db, claim)
    assert (row.status, row.approval_stage) == ("pending_approval", "final")
    assert await _approvals(db, claim) == [world.treasurer.id]
    assert await _entries(db, claim) == []


async def test_simultaneous_requests_from_two_people_need_both(db, world, submit, act):
    claim = await submit(world.volunteer, "1800", world.fest)
    results = await asyncio.gather(
        act(world.treasurer, claim), act(world.president, claim), act(world.treasurer, claim), act(world.president, claim)
    )
    assert sorted(response.status_code for response in results) == [200, 200, 409, 409]

    row = await _row(db, claim)
    assert row.status == "approved"
    assert {row.first_approved_by, row.final_approver_id} == {world.treasurer.id, world.president.id}
    assert sorted(await _approvals(db, claim), key=str) == sorted([world.treasurer.id, world.president.id], key=str)
    assert len(await _entries(db, claim)) == 1


# --- 14: the trail -------------------------------------------------------------------------------------------


async def test_approval_and_audit_history(client, db, world, auth, submit, act):
    claim = await submit(world.volunteer, "1800", world.fest)
    await act(world.treasurer, claim)
    await act(world.treasurer, claim)                      # refused: leaves no trace
    await act(world.president, claim, comment="Checked against the quote")
    await act(world.treasurer, claim, "mark-paid")

    history = (await client.get(f"{API}/{claim['id']}", headers=auth(world.volunteer))).json()["approval_history"]
    assert [(log["action"], log["approver_name"], log["approver_role"]) for log in history] == [
        ("submitted", "Vik Volunteer", "volunteer"),
        ("approved", "Tara Treasurer", "treasurer"),
        ("approved", "Priya President", "president"),
        ("paid", "Tara Treasurer", "treasurer"),
    ]
    assert history[1]["comment"] == "First approval. Final approval required."
    assert history[2]["comment"] == "Checked against the quote"

    [entry] = await _entries(db, claim)
    audit = list(await db.scalars(select(AuditLog).where(AuditLog.entity_id == entry.id)))
    assert [(row.action, row.details, row.actor_id) for row in audit] == [
        ("ledger.payment_status_changed", {"from": "approved", "to": "paid"}, world.treasurer.id)
    ]


async def test_rejecting_at_the_final_stage(db, world, submit, act):
    # The first approver may still stop a claim they approved; it never reaches the ledger.
    claim = await submit(world.volunteer, "1800", world.fest)
    await act(world.treasurer, claim)
    rejected = await act(world.treasurer, claim, "reject", reason="Quote was withdrawn")
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
    assert (await act(world.president, claim)).status_code == 409
    assert await _entries(db, claim) == []


# --- 15: records approved under the previous rule ---------------------------------------------------------------


async def test_claims_already_approved_by_one_person_are_left_alone(client, db, world, auth, submit, act):
    """A large claim approved by a single treasurer under the earlier rule is not rewritten."""
    claim = await submit(world.volunteer, "1800", world.fest)
    # Recreate what the earlier rule stored: approved in one step, one approver, entry posted.
    await db.execute(text(
        "UPDATE expense_claims SET status = 'approved', approval_stage = 'first', final_approver_id = :who, "
        "current_approver_id = NULL WHERE id = :id"), {"who": world.treasurer.id, "id": claim["id"]})
    await db.execute(text(
        "INSERT INTO approval_logs (expense_claim_id, approver_id, approver_role, action) "
        "VALUES (:id, :who, 'treasurer', 'approved')"), {"who": world.treasurer.id, "id": claim["id"]})
    await db.execute(text(
        "INSERT INTO ledger_entries (club_id, entry_type, source_module, reference_id, reference_type, amount, "
        "direction, payment_status, created_by, approved_by) VALUES (:club, 'reimbursement', 'expense_claim', :id, "
        "'expense_claim', 1800, 'debit', 'approved', :who, :who)"),
        {"club": world.club.id, "who": world.treasurer.id, "id": claim["id"]})
    await db.commit()
    before = (await _row(db, claim)).updated_at

    # Reading it, listing queues and trying to approve again change nothing.
    view = (await client.get(f"{API}/{claim['id']}", headers=auth(world.president))).json()
    assert (view["status"], view["first_approved_by"], view["final_approver_name"]) == ("approved", None, "Tara Treasurer")
    await client.get(f"{API}/approvals", headers=auth(world.president))
    assert (await act(world.president, claim)).status_code == 409

    row = await _row(db, claim)
    assert (row.status, row.first_approved_by, row.final_approver_id, row.updated_at) == (
        "approved", None, world.treasurer.id, before)
    assert await _approvals(db, claim) == [world.treasurer.id]
    [entry] = await _entries(db, claim)
    assert (entry.amount, entry.payment_status) == (Decimal("1800.00"), "approved")


# --- Pages -------------------------------------------------------------------------------------------------------


async def test_pages_show_each_approval_stage(client, world, auth, submit, act):
    def sign_in(user):
        client.cookies.set(get_settings().session_cookie, create_access_token(user.id))

    claim = await submit(world.volunteer, "1800", world.fest)
    page = f"/expenses/{claim['id']}"

    sign_in(world.volunteer)
    assert "Awaiting first approval from Lina Lead" in (await client.get("/expenses")).text
    detail = (await client.get(page)).text
    assert "2. First approval" in detail and "3. Final approval" in detail and "4. Payment" in detail

    await act(world.treasurer, claim)
    label = "First approval by Tara Treasurer · awaiting final approval from Priya President"
    assert label in (await client.get("/expenses")).text
    detail = (await client.get(page)).text
    assert label in detail and "Approved by Tara Treasurer" in detail

    # The first approver: no approve button, a reason instead, and the claim listed as waiting on someone else.
    sign_in(world.treasurer)
    detail = (await client.get(page)).text
    assert "You cannot approve this claim" in detail and "different treasurer or president" in detail
    assert f"{API}/{claim['id']}/approve" not in detail and f"{API}/{claim['id']}/reject" in detail
    queue = (await client.get("/expenses/approvals")).text
    assert "You gave the first approval (1)" in queue and f"{API}/{claim['id']}/approve" not in queue

    # A different finance admin gets the final-approval button.
    sign_in(world.president)
    queue = (await client.get("/expenses/approvals")).text
    assert f"{API}/{claim['id']}/approve" in queue and "Give final approval" in queue and "First: Tara Treasurer" in queue

    await act(world.president, claim)
    sign_in(world.volunteer)
    assert "Approved · awaiting payment" in (await client.get("/expenses")).text
    assert "Approved by Priya President" in (await client.get(page)).text
    await act(world.treasurer, claim, "mark-paid")
    assert "Paid to Vik Volunteer" in (await client.get(page)).text
