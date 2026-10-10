"""The Python ticket-price calculator must agree with MemberCore's TypeScript original.

tests/fixtures/ticket_discount_vectors.json is written by running the TypeScript
calculator itself (`make fairseat-vectors`). Every vector is replayed here.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.services.fairseat_pricing import EXPLANATIONS, Membership, calculate_ticket_discount

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "ticket_discount_vectors.json"
VECTORS = json.loads(FIXTURE.read_text())["vectors"]


def _run(vector_input: dict):
    raw = vector_input["membership"]
    membership = raw and Membership(
        status=raw["status"], club_id=raw["clubId"], starts_at_ms=raw["startsAtMs"],
        expires_at_ms=raw["expiresAtMs"], discount_bps=raw["discountBps"],
    )
    result = calculate_ticket_discount(
        base_price_paise=vector_input["basePricePaise"], event_club_id=vector_input["eventClubId"],
        membership=membership, now_ms=vector_input["nowMs"],
    )
    return {
        "eligible": result.eligible, "discountApplied": result.discount_applied, "reason": result.reason,
        "explanation": result.explanation, "appliedDiscountBps": result.applied_discount_bps,
        "pricePaise": result.price_paise, "savingsPaise": result.savings_paise,
    }


def test_every_shared_vector_matches_the_typescript_calculator():
    assert len(VECTORS) >= 350
    mismatches = []
    for vector in VECTORS:
        if "error" in vector:
            with pytest.raises(ValueError):
                _run(vector["input"])
        elif _run(vector["input"]) != vector["result"]:
            mismatches.append(vector["input"])
    assert mismatches == []


def test_vectors_cover_rounding_edges_every_reason_and_rejections():
    accepted = [vector for vector in VECTORS if "result" in vector]
    assert {vector["result"]["reason"] for vector in accepted} == set(EXPLANATIONS)
    assert sum("error" in vector for vector in VECTORS) >= 10
    # Halves round up: 1 paise at 50% is 1 paise, 3 paise at 50% is 2 paise.
    halves = {v["input"]["basePricePaise"]: v["result"]["pricePaise"] for v in accepted
              if v["result"]["appliedDiscountBps"] == 5000}
    assert (halves[1], halves[3], halves[51]) == (1, 2, 26)
    assert max(v["input"]["basePricePaise"] for v in accepted) == 2**53 - 1


def test_fixture_is_what_the_typescript_source_produces_today(tmp_path):
    """Regenerate from the TypeScript source and compare, when node is available."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; the committed fixture is used as it stands")
    version = subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip().lstrip("v")
    if tuple(int(part) for part in version.split(".")[:2]) < (22, 6):
        pytest.skip("node is too old to run TypeScript directly")
    before = FIXTURE.read_bytes()
    try:
        subprocess.run(
            [node, "--experimental-strip-types", "--no-warnings", "scripts/make_ticket_discount_vectors.mjs"],
            cwd=ROOT, check=True, capture_output=True,
        )
        assert FIXTURE.read_bytes() == before, "ticket-discount.ts changed: run `make fairseat-vectors` and review"
    finally:
        FIXTURE.write_bytes(before)
