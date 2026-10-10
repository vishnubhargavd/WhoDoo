"""Receipt OCR with Tesseract, and the rules that pull vendor, amount and date out of the text.

OCR is a convenience, never a requirement: if Tesseract is missing, the file is a
PDF, or the image is unreadable, `read_receipt` returns an empty result with zero
confidence and the volunteer types the values in.
"""

import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

logger = logging.getLogger("whodoo.ocr")

NUMBER = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)(?![\w%])")
DECIMAL = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{2,3})+\.\d{2}|\d+\.\d{2})(?![\w%])")

# Lines that are never the shop's name: a bare document label, a labelled detail, or an address.
GENERIC_HEADING = re.compile(
    r"^(tax\s+)?(invoice|receipt|bill|cash\s+memo|estimate|quotation|original|duplicate|copy|"
    r"bill\s+of\s+supply|welcome|thank\s+you)(\s+(copy|for\s+recipient))?$", re.I)
LABELLED_DETAIL = re.compile(
    r"\b(gstin|gst\s*no|fssai|cin|pan|phone|ph|tel|mob(ile)?|email|bill\s*no|invoice\s*no|"
    r"order\s*no|date|time|table|cashier|www\.)\b|@", re.I)
ADDRESS_WORD = re.compile(
    r"\b(road|rd|street|st|nagar|lane|floor|near|opp|colony|sector|block|plot|main|cross|camp|"
    r"square|chowk|market|complex|building|tower|layout|phase)\b|\b\d{6}\b", re.I)

# Summary lines below the items.
SUBTOTAL = re.compile(r"\bsub\s*-?\s*total\b", re.I)
TAX = re.compile(r"\b(igst|cgst|sgst|utgst|gst|vat|cess|tax|service\s+charge)\b", re.I)
DISCOUNT = re.compile(r"\b(discount|coupon|offer|less)\b", re.I)
# Money that changed hands, not the bill: never an item and never the total.
TENDER = re.compile(
    r"\b(cash|tender(ed)?|change|card|upi|wallet|balance\s+returned|paid\s+by|received|round(ing)?\s*off|savings?)\b",
    re.I)
NOT_THE_TOTAL = re.compile(r"\btotal\s+(items?|qty|quantity|tax|gst|savings?|discount|pieces|pcs)\b", re.I)
# Checked in order: an explicit final-total label beats a plain "total".
TOTAL_LABELS = (
    re.compile(
        r"\b(grand\s*total|net\s*(total|amount|payable)|amount\s*(payable|due)|total\s*(amount|payable|due)|"
        r"bill\s*amount|invoice\s*total|balance\s*due)\b", re.I),
    re.compile(r"\btotal\b", re.I),
)
TABLE_HEADER = re.compile(r"\b(item|description|particulars?)\b.*\b(amt|amount|price|rate|total)\b", re.I)
# "1 Orange 1 400.00 448.00": serial no, name, quantity, unit price, line amount.
ITEM_WITH_QTY = re.compile(
    r"^(?:\d{1,3}[.)]?\s+)?(?P<name>.*?[A-Za-z].*?)\s+(?P<qty>\d{1,4}(?:\.\d{1,3})?|[iIl|!])\s*(?:x|@|pcs?|nos?)?\s+"
    r"(?P<price>[\d,]+\.\d{2})\s+(?P<amount>[\d,]+[. ]\d{2})$", re.I)  # OCR sometimes drops the last dot
ITEM_SIMPLE = re.compile(r"^(?:\d{1,3}[.)]\s+)?(?P<name>.*?[A-Za-z].*?)\s+(?:rs\.?|inr|₹)?\s*(?P<amount>[\d,]+\.\d{2})$", re.I)

MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
DATE_NUMERIC = re.compile(r"\b(\d{1,2})\s?[/\-.]\s?(\d{1,2})\s?[/\-.]\s?(\d{4}|\d{2})\b")
DATE_ISO = re.compile(r"\b(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})\b")
DATE_DAY_FIRST = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?[\s\-.,]+([A-Za-z]{3,9})[\s\-.,]+(\d{4}|\d{2})\b")
DATE_MONTH_FIRST = re.compile(r"\b([A-Za-z]{3,9})[\s\-.]+(\d{1,2})(?:st|nd|rd|th)?[\s,]+(\d{4})\b")

ONE_RUPEE = Decimal("1.00")


@dataclass
class LineItem:
    description: str
    amount: Decimal
    quantity: Decimal | None = None
    unit_price: Decimal | None = None

    def as_json(self) -> dict:
        return {
            "description": self.description,
            "quantity": str(self.quantity) if self.quantity is not None else None,
            "unit_price": str(self.unit_price) if self.unit_price is not None else None,
            "amount": str(self.amount),
        }


@dataclass
class ParsedReceipt:
    """What the rules could read from a receipt's text. Anything not read stays None: nothing is guessed."""

    vendor: str | None = None
    expense_date: date | None = None
    total: Decimal | None = None             # only ever taken from a line labelled as the total
    subtotal: Decimal | None = None
    tax_total: Decimal | None = None
    discount_total: Decimal | None = None
    items: list[LineItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def items_total(self) -> Decimal | None:
        return sum((item.amount for item in self.items), Decimal("0.00")) if self.items else None

    @property
    def suggested_total(self) -> Decimal | None:
        """A figure to offer the user when the total line could not be read. Never used without their say-so."""
        if self.total is not None:
            return None
        candidates = set()
        if self.items_total is not None:
            candidates.add(self.items_total)
        if self.subtotal is not None:
            candidates.add(self.subtotal + (self.tax_total or 0) - (self.discount_total or 0))
        return candidates.pop() if len(candidates) == 1 else None

    def summary_json(self) -> dict:
        def text(value):
            return str(value) if value is not None else None

        return {
            "vendor": self.vendor,
            "date": self.expense_date.isoformat() if self.expense_date else None,
            "total": text(self.total),
            "subtotal": text(self.subtotal),
            "tax_total": text(self.tax_total),
            "discount_total": text(self.discount_total),
            "items_total": text(self.items_total),
            "suggested_total": text(self.suggested_total),
            "line_items": [item.as_json() for item in self.items],
            "warnings": self.warnings,
        }


@dataclass
class OcrResult:
    available: bool = False
    confidence: Decimal = Decimal("0")
    vendor: str | None = None
    amount: Decimal | None = None
    expense_date: date | None = None
    raw: dict = field(default_factory=dict)
    parsed: ParsedReceipt = field(default_factory=ParsedReceipt)


def _to_amount(text: str) -> Decimal | None:
    try:
        value = Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None
    return value.quantize(Decimal("0.01")) if 0 < value < Decimal("10000000") else None


def _amounts_in(line: str) -> list[Decimal]:
    return [amount for match in NUMBER.findall(line) if (amount := _to_amount(match)) is not None]


def _last_decimal(line: str) -> Decimal | None:
    """The right-most figure written with paise, which is where receipts print a line's amount."""
    found = [amount for match in DECIMAL.findall(line) if (amount := _to_amount(match)) is not None]
    return found[-1] if found else None


def _total_on(lines: list[str], index: int) -> Decimal | None:
    """The amount on a total line, or alone on the line right below it (OCR often splits the two)."""
    amounts = _amounts_in(lines[index])
    if amounts:
        return amounts[-1]
    if index + 1 < len(lines):
        below = lines[index + 1].strip()
        if re.fullmatch(r"(?:rs\.?|inr|₹|=|~|:)?\s*[\d,]+(?:\.\d{1,2})?", below, re.I):
            return (_amounts_in(below) or [None])[-1]
    return None


def _is_total_line(line: str) -> bool:
    return bool(
        TOTAL_LABELS[1].search(line) and not SUBTOTAL.search(line) and not NOT_THE_TOTAL.search(line)
        and not TENDER.search(line)
    )


def extract_total(lines: list[str]) -> tuple[Decimal | None, bool]:
    """Return (total, label_seen). The total only ever comes from a line labelled as one.

    If a total label is there but its number is unreadable, the answer is (None, True):
    we do not fall back to some other price on the receipt.
    """
    label_seen = False
    for label in TOTAL_LABELS:
        for index in range(len(lines) - 1, -1, -1):
            line = lines[index]
            if label.search(line) and _is_total_line(line):
                label_seen = True
                amount = _total_on(lines, index)
                if amount is not None:
                    return amount, True
    return None, label_seen


def extract_amount(lines: list[str]) -> Decimal | None:
    return extract_total(lines)[0]


def extract_items(lines: list[str]) -> list[LineItem]:
    """Read the item lines, which sit above the first subtotal, tax or total line."""
    items: list[LineItem] = []
    for line in lines:
        if SUBTOTAL.search(line) or _is_total_line(line):
            break
        with_qty = ITEM_WITH_QTY.match(line)
        if with_qty:
            amount, price = _to_amount(with_qty["amount"].replace(" ", ".")), _to_amount(with_qty["price"])
            if amount is not None:
                # A quantity misread as a letter ("i" for "1") is left blank rather than guessed.
                quantity = Decimal(with_qty["qty"]) if with_qty["qty"][0].isdigit() else None
                items.append(LineItem(_clean(with_qty["name"]), amount, quantity, price))
            continue
        if TAX.search(line) and _last_decimal(line) is not None:
            if items:
                break  # the tax lines start the summary block
            continue
        if LABELLED_DETAIL.search(line) or TENDER.search(line) or DISCOUNT.search(line) or TABLE_HEADER.search(line):
            continue
        simple = ITEM_SIMPLE.match(line)
        if simple and (amount := _to_amount(simple["amount"])) is not None:
            items.append(LineItem(_clean(simple["name"]), amount))
        elif items and re.fullmatch(r"[A-Za-z][A-Za-z %&.'/-]{1,30}", line) and not GENERIC_HEADING.match(line):
            items[-1].description += " " + line  # a wrapped item name, e.g. "Orange" / "Powder"
    return items


def _sum_matching(lines: list[str], pattern: re.Pattern, *, skip_items: bool = True) -> Decimal | None:
    found = [
        amount
        for line in lines
        if pattern.search(line) and not (skip_items and ITEM_WITH_QTY.match(line))
        and not _is_total_line(line) and not SUBTOTAL.search(line)
        and (amount := _last_decimal(line)) is not None
    ]
    return sum(found, Decimal("0.00")) if found else None


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w&'.,%/\- ]", " ", text)).strip(" .,-")


def _build_date(day: int, month: int, year: int, today: date) -> date | None:
    if year < 100:
        year += 2000
    try:
        candidate = date(year, month, day)
    except ValueError:
        return None
    # A receipt is never from the future, and anything very old is a misread.
    if candidate > today + timedelta(days=1) or candidate.year < today.year - 5:
        return None
    return candidate


def extract_date(lines: list[str], today: date | None = None) -> date | None:
    """Find the first plausible date. Numeric dates are read day-first (Indian convention)."""
    today = today or date.today()
    for line in lines:
        for match in DATE_ISO.finditer(line):
            year, month, day = (int(part) for part in match.groups())
            if found := _build_date(day, month, year, today):
                return found
        for match in DATE_NUMERIC.finditer(line):
            day, month, year = (int(part) for part in match.groups())
            if found := _build_date(day, month, year, today):
                return found
        # "23 - Jan - 2025": squeeze the spaces around separators before matching written months.
        squeezed = re.sub(r"\s*([\-/.])\s*", r"\1", line)
        for match in DATE_DAY_FIRST.finditer(squeezed):
            month = MONTHS.get(match.group(2)[:3].lower())
            if month and (found := _build_date(int(match.group(1)), month, int(match.group(3)), today)):
                return found
        for match in DATE_MONTH_FIRST.finditer(squeezed):
            month = MONTHS.get(match.group(1)[:3].lower())
            if month and (found := _build_date(int(match.group(2)), month, int(match.group(3)), today)):
                return found
    return None


def extract_vendor(lines: list[str]) -> str | None:
    """The shop name: the first line near the top that is a name, not a label, detail or address."""
    for line in lines[:6]:
        cleaned = _clean(line)
        letters = sum(char.isalpha() for char in cleaned)
        if letters < 3 or letters < len(cleaned) * 0.6:
            continue
        if GENERIC_HEADING.match(cleaned) or LABELLED_DETAIL.search(cleaned):
            continue
        if cleaned.count(",") >= 2 or ADDRESS_WORD.search(cleaned) or DECIMAL.search(line):
            continue
        return cleaned[:120].title() if cleaned.isupper() else cleaned[:120]
    return None


def parse_receipt(text: str, today: date | None = None) -> ParsedReceipt:
    """Turn OCR text into fields. Missing or doubtful values are left empty and explained in `warnings`."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    total, label_seen = extract_total(lines)
    receipt = ParsedReceipt(
        vendor=extract_vendor(lines),
        expense_date=extract_date(lines, today),
        total=total,
        subtotal=next((amount for line in lines if SUBTOTAL.search(line) and (amount := _last_decimal(line))), None),
        tax_total=_sum_matching(lines, TAX),
        discount_total=_sum_matching(lines, DISCOUNT),
        items=extract_items(lines),
    )

    if receipt.vendor is None and lines:
        receipt.warnings.append("The merchant name could not be read.")
    if receipt.expense_date is None and lines:
        receipt.warnings.append("The date could not be read.")
    if total is None and lines:
        reason = "The total line could not be read." if label_seen else "No total line was found on the receipt."
        if receipt.suggested_total is not None:
            reason += f" The readable lines add up to {receipt.suggested_total:.2f}; check it against the receipt."
        receipt.warnings.append(reason)
    elif total is not None and receipt.items:
        tax, discount = receipt.tax_total or Decimal(0), receipt.discount_total or Decimal(0)

        def matches(*figures: Decimal) -> bool:
            return any(abs(total - figure) <= ONE_RUPEE for figure in figures)

        items = receipt.items_total
        if matches(items, items + tax - discount, items - discount):
            pass  # the item lines account for the total
        elif receipt.subtotal is not None and matches(receipt.subtotal + tax - discount):
            # The receipt's own summary adds up, so the total is sound; we just missed some items.
            receipt.warnings.append(
                f"Only {len(receipt.items)} item lines could be read ({items:.2f} of {total:.2f})."
            )
        else:
            receipt.warnings.append(
                f"The total ({total:.2f}) does not match the {len(receipt.items)} readable item lines "
                f"({items:.2f}). Some lines may have been misread."
            )
    return receipt


def parse_receipt_text(text: str, today: date | None = None) -> tuple[str | None, Decimal | None, date | None]:
    receipt = parse_receipt(text, today)
    return receipt.vendor, receipt.total, receipt.expense_date


def _fallback(reason: str) -> OcrResult:
    return OcrResult(raw={"engine": None, "reason": reason, "at": datetime.now().isoformat(timespec="seconds")})


# Page segmentation modes to try, in order. 4 (one column of text) suits most receipts;
# 6 (one uniform block) rescues layouts where 4 drops lines.
SEGMENTATION_MODES = (4, 6)
TARGET_WIDTH = 1200
MAX_HEIGHT = 6000


def _read_once(image, mode: int) -> OcrResult | None:
    import pytesseract

    words = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT, config=f"--psm {mode}")
    lines: dict[tuple, list[str]] = {}
    confidences: list[float] = []
    for index, word in enumerate(words["text"]):
        confidence = float(words["conf"][index])
        if not word.strip() or confidence < 0:
            continue
        key = (words["block_num"][index], words["par_num"][index], words["line_num"][index])
        lines.setdefault(key, []).append(word)
        confidences.append(confidence)
    if not confidences:
        return None

    text = "\n".join(" ".join(parts) for parts in lines.values())
    parsed = parse_receipt(text)
    mean_confidence = Decimal(str(sum(confidences) / len(confidences))).quantize(Decimal("0.01"))
    return OcrResult(
        available=True,
        confidence=mean_confidence,
        vendor=parsed.vendor,
        amount=parsed.total,
        expense_date=parsed.expense_date,
        parsed=parsed,
        raw={"engine": "tesseract", "segmentation_mode": mode, "text": text, "word_count": len(confidences),
             "mean_word_confidence": float(mean_confidence), "parsed": parsed.summary_json()},
    )


def _fields_found(result: OcrResult) -> tuple[int, int]:
    """How much a reading recovered: the three key fields first, then the number of item lines."""
    fields = sum(value is not None for value in (result.vendor, result.amount, result.expense_date))
    return fields, len(result.parsed.items)


def read_receipt(data: bytes, content_type: str) -> OcrResult:
    """Run OCR on an uploaded receipt. Blocking: call it through a thread pool."""
    if content_type == "application/pdf":
        return _fallback("pdf_not_supported")
    try:
        from PIL import Image, ImageOps

        image = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
        if image.mode in ("RGBA", "LA", "P"):  # put transparent screenshots on white, not black
            image = image.convert("RGBA")
            page = Image.new("RGBA", image.size, "white")
            page.alpha_composite(image)
            image = page
        image = image.convert("L")
        # Receipts are tall and narrow, so size by WIDTH: text needs roughly 1,200 px across
        # to be read reliably. (Sizing by the longest side left narrow receipts too small,
        # and the small print at the bottom, including the total, was lost.)
        if image.width < TARGET_WIDTH:
            scale = min(TARGET_WIDTH / image.width, MAX_HEIGHT / image.height)
            if scale > 1:
                image = image.resize((round(image.width * scale), round(image.height * scale)), Image.LANCZOS)
        image = ImageOps.autocontrast(image)

        best: OcrResult | None = None
        for mode in SEGMENTATION_MODES:
            result = _read_once(image, mode)
            if result and (best is None or _fields_found(result) > _fields_found(best)):
                best = result
            if best and _fields_found(best)[0] == 3 and best.parsed.items:
                break
    except Exception as exc:  # Tesseract missing, corrupt image, timeout: all mean "type it in"
        logger.warning("OCR unavailable: %s", exc)
        return _fallback(f"ocr_failed: {type(exc).__name__}")
    return best or _fallback("no_text_found")
