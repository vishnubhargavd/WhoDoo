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
NOT_A_VENDOR = re.compile(
    r"\b(invoice|receipt|bill|tax|gstin?|date|time|phone|tel|mob|cash memo|original|duplicate|copy)\b", re.I
)
# Checked in order: the first kind of line that yields a number wins.
TOTAL_LABELS = (
    re.compile(r"\b(grand\s*total|net\s*(amount|payable|total)|amount\s*(payable|paid|due)|total\s*(amount|payable|due))\b", re.I),
    re.compile(r"(?<!sub)(?<!sub )(?<!sub-)\btotal\b", re.I),
    re.compile(r"\b(amount|paid)\b", re.I),
)
MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
DATE_NUMERIC = re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4}|\d{2})\b")
DATE_ISO = re.compile(r"\b(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})\b")
DATE_DAY_FIRST = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?[\s\-.,]+([A-Za-z]{3,9})[\s\-.,]+(\d{4}|\d{2})\b")
DATE_MONTH_FIRST = re.compile(r"\b([A-Za-z]{3,9})[\s\-.]+(\d{1,2})(?:st|nd|rd|th)?[\s,]+(\d{4})\b")


@dataclass
class OcrResult:
    available: bool = False
    confidence: Decimal = Decimal("0")
    vendor: str | None = None
    amount: Decimal | None = None
    expense_date: date | None = None
    raw: dict = field(default_factory=dict)


def _to_amount(text: str) -> Decimal | None:
    try:
        value = Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None
    return value.quantize(Decimal("0.01")) if 0 < value < Decimal("10000000") else None


def _amounts_in(line: str) -> list[Decimal]:
    return [amount for match in NUMBER.findall(line) if (amount := _to_amount(match)) is not None]


def extract_amount(lines: list[str]) -> Decimal | None:
    """Prefer the number on a "total" line; otherwise the largest figure written with paise."""
    for label in TOTAL_LABELS:
        for line in reversed(lines):
            if label.search(line):
                amounts = _amounts_in(line)
                if amounts:
                    return amounts[-1]
    with_paise = [
        amount
        for line in lines
        for match in NUMBER.findall(line)
        if "." in match and (amount := _to_amount(match)) is not None
    ]
    return max(with_paise) if with_paise else None


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
        for match in DATE_DAY_FIRST.finditer(line):
            month = MONTHS.get(match.group(2)[:3].lower())
            if month and (found := _build_date(int(match.group(1)), month, int(match.group(3)), today)):
                return found
        for match in DATE_MONTH_FIRST.finditer(line):
            month = MONTHS.get(match.group(1)[:3].lower())
            if month and (found := _build_date(int(match.group(2)), month, int(match.group(3)), today)):
                return found
    return None


def extract_vendor(lines: list[str]) -> str | None:
    """The shop name is almost always the first proper line of text at the top of a receipt."""
    for line in lines[:6]:
        cleaned = re.sub(r"[^\w&'.,\- ]", " ", line)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,-")
        letters = sum(char.isalpha() for char in cleaned)
        if letters >= 4 and letters >= len(cleaned) * 0.6 and not NOT_A_VENDOR.search(cleaned):
            return cleaned[:120].title() if cleaned.isupper() else cleaned[:120]
    return None


def parse_receipt_text(text: str, today: date | None = None) -> tuple[str | None, Decimal | None, date | None]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return extract_vendor(lines), extract_amount(lines), extract_date(lines, today)


def _fallback(reason: str) -> OcrResult:
    return OcrResult(raw={"engine": None, "reason": reason, "at": datetime.now().isoformat(timespec="seconds")})


# Page segmentation modes to try, in order. 4 (one column of text) suits most receipts;
# 6 (one uniform block) rescues layouts where 4 drops lines.
SEGMENTATION_MODES = (4, 6)


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
    vendor, amount, expense_date = parse_receipt_text(text)
    mean_confidence = Decimal(str(sum(confidences) / len(confidences))).quantize(Decimal("0.01"))
    return OcrResult(
        available=True,
        confidence=mean_confidence,
        vendor=vendor,
        amount=amount,
        expense_date=expense_date,
        raw={"engine": "tesseract", "segmentation_mode": mode, "text": text, "word_count": len(confidences),
             "mean_word_confidence": float(mean_confidence)},
    )


def _fields_found(result: OcrResult) -> int:
    return sum(value is not None for value in (result.vendor, result.amount, result.expense_date))


def read_receipt(data: bytes, content_type: str) -> OcrResult:
    """Run OCR on an uploaded receipt. Blocking: call it through a thread pool."""
    if content_type == "application/pdf":
        return _fallback("pdf_not_supported")
    try:
        from PIL import Image, ImageOps

        image = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("L")
        if max(image.size) < 1400:  # small phone crops read much better when enlarged
            scale = 1400 / max(image.size)
            image = image.resize((round(image.width * scale), round(image.height * scale)))
        image = ImageOps.autocontrast(image)

        best: OcrResult | None = None
        for mode in SEGMENTATION_MODES:
            result = _read_once(image, mode)
            if result and (best is None or _fields_found(result) > _fields_found(best)):
                best = result
            if best and _fields_found(best) == 3:
                break
    except Exception as exc:  # Tesseract missing, corrupt image, timeout: all mean "type it in"
        logger.warning("OCR unavailable: %s", exc)
        return _fallback(f"ocr_failed: {type(exc).__name__}")
    return best or _fallback("no_text_found")
