"""Regression tests for receipt reading: merchant title, item lines and the final total.

Reported bugs: the merchant title was missing, and the first item's price was shown
where the receipt's total should have been.
"""

import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models.ledger import LedgerEntry
from app.services import ocr_service
from app.services.ocr_service import parse_receipt

TODAY = date(2026, 10, 10)
SAMPLES = Path(__file__).resolve().parents[1] / "app" / "static" / "samples"
MULTI_ITEM = (SAMPLES / "receipt_grocery_6_items_968.png").read_bytes()
API = "/api/v1/expenses"
needs_tesseract = pytest.mark.skipif(shutil.which("tesseract") is None, reason="Tesseract is not installed")

# The text Tesseract produced for the receipt in the bug report, once the image was scaled properly.
SLEEK_BILL = """SLEEK BILL
Nirmal Vijay, Panchshil Square, Tapovan Road,
Camp, Amravati, 444602, IN
PHONE : +911234567890
GSTIN : 27AAFCV2449G1Z7
Bill No: IN-15 Date: 23 - Jan - 2025
SN Item Qty Price Amt
1 Orange 1 400.00 448 00
Powder
2 Walnuts5% 1 100.00 105.00
Tax Item
3 Coin3%Tax 1 100.00 103.00
ltem
4 Rose Water 1 150.00 150.00
5  Glicerene 1 50.00 50.00
6 Cheese 12% 1 100.00 112.00
Tax Item
Subtotal 6 = 900.00
IGST at 0% 0.00
IGST at 3% 3.00
IGST at 5% 5.00
IGST at 12% 60.00
TOTAL = 968.00"""


def amounts(receipt) -> list[str]:
    return [str(item.amount) for item in receipt.items]


# --- Text rules ---------------------------------------------------------------------


def test_reported_receipt_title_items_and_total():
    receipt = parse_receipt(SLEEK_BILL, TODAY)

    # Bug A: "SLEEK BILL" was thrown away for containing the word "bill", and the address was used.
    assert receipt.vendor == "Sleek Bill"
    # Bugs B and C: the first item's amount (448.00) was reported as the receipt's amount.
    assert receipt.total == Decimal("968.00")
    assert receipt.total != receipt.items[0].amount

    assert amounts(receipt) == ["448.00", "105.00", "103.00", "150.00", "50.00", "112.00"]
    assert receipt.items[0].description == "Orange Powder"  # wrapped name joined; title untouched
    assert (receipt.items[0].quantity, receipt.items[0].unit_price) == (Decimal("1"), Decimal("400.00"))
    assert (receipt.subtotal, receipt.tax_total) == (Decimal("900.00"), Decimal("68.00"))
    assert receipt.items_total == Decimal("968.00")
    assert receipt.expense_date == date(2025, 1, 23)
    assert receipt.warnings == []


def test_receipt_with_one_item():
    receipt = parse_receipt("Quick Print Hub\nDate: 05/10/2026\nPosters A3 x 20 260.00\nTOTAL Rs. 260.00", TODAY)
    assert (receipt.vendor, receipt.total, amounts(receipt)) == ("Quick Print Hub", Decimal("260.00"), ["260.00"])
    assert receipt.warnings == []


def test_receipt_with_several_items_and_an_explicit_grand_total():
    receipt = parse_receipt(
        "FESTIVE DECOR HOUSE\nInvoice 2093 Date: 28-09-2026\nStage backdrop cloth 1400.00\n"
        "LED string lights x 6 780.00\nBalloons and tape 320.00\nGRAND TOTAL Rs. 2500.00", TODAY)
    assert receipt.vendor == "Festive Decor House"
    assert amounts(receipt) == ["1400.00", "780.00", "320.00"]
    assert receipt.total == Decimal("2500.00") == receipt.items_total


def test_subtotal_tax_discount_and_final_total_are_told_apart():
    receipt = parse_receipt(
        "GREEN MART\n02/10/2026\nMilk 1 54.00 54.00\nBread 2 40.00 80.00\nEggs 1 78.00 78.00\n"
        "Sub Total 212.00\nCGST 2.5% 5.30\nSGST 2.5% 5.30\nDiscount 10.00\nTotal 212.60\nGrand Total 212.60\n"
        "Cash 500.00\nChange 287.40\nCard XXXX 4321\nPh 9876543210", TODAY)
    assert receipt.subtotal == Decimal("212.00")
    assert receipt.tax_total == Decimal("10.60")
    assert receipt.discount_total == Decimal("10.00")
    assert receipt.total == Decimal("212.60")  # not the subtotal, cash (500.00) or change (287.40)
    assert amounts(receipt) == ["54.00", "80.00", "78.00"]  # tax, cash and change are not items
    assert receipt.items[1].quantity * receipt.items[1].unit_price == receipt.items[1].amount
    assert receipt.warnings == []


def test_first_listed_price_is_never_used_as_the_total():
    text = "Campus Stationers\n01/10/2026\nMarkers 999.00\nChart paper 40.00\nTape 25.00\nTotal 1064.00"
    receipt = parse_receipt(text, TODAY)
    assert receipt.total == Decimal("1064.00")
    assert receipt.items[0].amount == Decimal("999.00")


@pytest.mark.parametrize("total_line, reason", [
    ("TOTAL stst=<~*s~‘s~sSCS*«CB_OD", "The total line could not be read."),  # the garbled line from the bug report
    ("", "No total line was found on the receipt."),
])
def test_missing_or_unreadable_total_is_left_empty(total_line, reason):
    receipt = parse_receipt(f"Cafe Mocha\n01/10/2026\nCoffee 2 90.00 180.00\nSandwich 1 120.00 120.00\n{total_line}", TODAY)
    # The old code fell back to the largest item price here. Now: nothing is invented.
    assert receipt.total is None
    assert receipt.suggested_total == Decimal("300.00")  # offered to the user, not applied
    assert receipt.warnings[-1].startswith(reason)
    assert "300.00" in receipt.warnings[-1]


def test_total_split_onto_the_next_line_is_still_read():
    assert parse_receipt("Shop\nPens 100.00\nTOTAL\nRs. 100.00", TODAY).total == Decimal("100.00")
    assert parse_receipt("Shop\nPens 100.00\nTotal items 1\nTotal 100", TODAY).total == Decimal("100.00")


def test_total_that_disagrees_with_the_items_is_flagged():
    receipt = parse_receipt("Shop One\n01/10/2026\nPens 100.00\nPaper 200.00\nTotal 900.00", TODAY)
    assert receipt.total == Decimal("900.00")  # reported as printed, but flagged
    assert "does not match" in receipt.warnings[0]


def test_missing_merchant_is_reported_not_guessed():
    receipt = parse_receipt("Tax Invoice\nGSTIN: 29ABCDE1234F1Z5\nPh: 9876543210\n12 MG Road, Bengaluru 560001\nTea 20.00\nTotal 20.00", TODAY)
    assert receipt.vendor is None
    assert "merchant name could not be read" in receipt.warnings[0]
    assert parse_receipt("", TODAY).vendor is None and parse_receipt("", TODAY).warnings == []


def test_merchant_title_survives_generic_words_and_item_lines():
    assert parse_receipt("TAX INVOICE\nCafe Mocha\nLatte 120.00\nTotal 120.00", TODAY).vendor == "Cafe Mocha"
    assert parse_receipt("The Receipt Shop\nPens 10.00\nTotal 10.00", TODAY).vendor == "The Receipt Shop"
    assert parse_receipt("Latte 120.00\nTotal 120.00", TODAY).vendor is None  # an item line is not a title


# --- Image to API -------------------------------------------------------------------


@needs_tesseract
def test_narrow_multi_item_image_is_read():
    """A small, narrow receipt image: sizing by the longest side left the text too small to read the total."""
    result = ocr_service.read_receipt(MULTI_ITEM, "image/png")
    assert result.available
    assert result.vendor == "Nature Basket Stores"
    assert result.amount == Decimal("968.00")
    assert result.expense_date == date(2026, 9, 23)
    assert len(result.parsed.items) == 6
    assert result.parsed.items[0].amount == Decimal("448.00") != result.amount
    assert (result.parsed.subtotal, result.parsed.tax_total) == (Decimal("900.00"), Decimal("68.00"))


@needs_tesseract
def test_transparent_png_is_read():
    import io

    from PIL import Image, ImageOps

    # White becomes fully transparent, as in a screenshot saved without a background.
    image = Image.open(io.BytesIO(MULTI_ITEM)).convert("RGBA")
    image.putalpha(ImageOps.invert(image.convert("L")))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    assert ocr_service.read_receipt(buffer.getvalue(), "image/png").amount == Decimal("968.00")


@needs_tesseract
async def test_ocr_api_returns_items_and_total(client, world, auth):
    response = await client.post(
        f"{API}/ocr", headers=auth(world.volunteer), files={"file": ("bill.png", MULTI_ITEM, "image/png")}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["extracted_vendor"] == "Nature Basket Stores"
    assert body["extracted_amount"] == 968.0
    assert body["amount_status"] == "read_from_total_line"
    assert [item["amount"] for item in body["line_items"]] == [448.0, 105.0, 103.0, 150.0, 50.0, 112.0]
    assert (body["subtotal"], body["tax_total"], body["items_total"]) == (900.0, 68.0, 968.0)
    assert body["suggested_amount"] is None
    assert 0 < body["ocr_confidence"] <= 100


async def test_unreadable_total_needs_the_user_to_enter_it(client, db, world, auth, monkeypatch):
    parsed = parse_receipt("Cafe Mocha\n01/10/2026\nCoffee 2 90.00 180.00\nSandwich 1 120.00 120.00\nTOTAL =~~", TODAY)
    reading = ocr_service.OcrResult(
        available=True, confidence=Decimal("81.5"), vendor=parsed.vendor, amount=parsed.total,
        expense_date=parsed.expense_date, parsed=parsed, raw={"engine": "tesseract", "parsed": parsed.summary_json()},
    )
    monkeypatch.setattr(ocr_service, "read_receipt", lambda data, content_type: reading)

    upload = (await client.post(
        f"{API}/ocr", headers=auth(world.volunteer), files={"file": ("r.png", MULTI_ITEM, "image/png")}
    )).json()
    assert upload["extracted_amount"] is None and upload["amount_status"] == "not_found"
    assert upload["suggested_amount"] == 300.0
    assert [item["amount"] for item in upload["line_items"]] == [180.0, 120.0]
    assert "total line could not be read" in upload["warnings"][0]

    body = {"receipt_id": upload["receipt_id"], "title": "Team snacks", "description": "Coffee and sandwiches",
            "category": "food", "expense_date": "2026-10-01", "event_id": str(world.bake_sale.id)}
    # No amount, no claim: nothing is filled in on the user's behalf.
    assert (await client.post(API, headers=auth(world.volunteer), json=body)).status_code == 422

    claim = (await client.post(API, headers=auth(world.volunteer), json=body | {"amount": "300.00"})).json()
    assert (claim["amount"], claim["extracted_amount"], claim["amount_edited"]) == (300.0, None, False)
    assert [item["amount"] for item in claim["ocr_raw_response"]["parsed"]["line_items"]] == ["180.00", "120.00"]


@needs_tesseract
async def test_ledger_uses_the_confirmed_amount_not_the_ocr_reading(client, db, world, auth):
    upload = (await client.post(
        f"{API}/ocr", headers=auth(world.volunteer), files={"file": ("bill.png", MULTI_ITEM, "image/png")}
    )).json()
    assert upload["extracted_amount"] == 968.0

    # The volunteer claims only part of the bill (two items were personal) and corrects the amount.
    claim = (await client.post(API, headers=auth(world.volunteer), json={
        "receipt_id": upload["receipt_id"], "title": "Stall supplies", "description": "Club share of the bill",
        "category": "supplies", "amount": "665.00", "expense_date": "2026-09-23", "event_id": str(world.bake_sale.id),
    })).json()
    assert (claim["amount"], claim["extracted_amount"], claim["amount_edited"]) == (665.0, 968.0, True)

    # Reading and submitting post nothing to the ledger; only approval does.
    assert list(await db.scalars(select(LedgerEntry))) == []

    await client.post(f"{API}/{claim['id']}/approve", headers=auth(world.lead))
    [entry] = list(await db.scalars(select(LedgerEntry)))
    assert entry.amount == Decimal("665.00")  # not 968.00 (OCR) and not 448.00 (first item)
    assert (entry.payment_status, entry.entry_type) == ("approved", "reimbursement")

    await client.post(f"{API}/{claim['id']}/mark-paid", headers=auth(world.treasurer))
    summary = (await client.get("/api/v1/ledger/summary", headers=auth(world.treasurer))).json()
    assert summary["total_reimbursements_paid"] == 665.0
