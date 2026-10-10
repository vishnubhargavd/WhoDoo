"""Draw the sample receipts used in the demo and the OCR test.

    python -m scripts.make_sample_receipts

Writes PNGs to app/static/samples/. They are committed, so this only needs
re-running if the receipts change.
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT_DIR = Path(__file__).resolve().parents[1] / "app" / "static" / "samples"

RECEIPTS = {
    "receipt_bake_sale_850.png": [
        ("SWEET CRUMBS BAKERY SUPPLIES", "title"),
        ("12 MG Road, Bengaluru 560001", "small"),
        ("GSTIN 29ABCDE1234F1Z5", "small"),
        ("", "small"),
        ("Bill No: 4471          Date: 03/10/2026", "body"),
        ("----------------------------------------", "body"),
        ("Wheat flour 10 kg               320.00", "body"),
        ("Butter 2 kg                     280.00", "body"),
        ("Sugar 5 kg                      150.00", "body"),
        ("Paper boxes x 50                100.00", "body"),
        ("----------------------------------------", "body"),
        ("TOTAL                       Rs. 850.00", "title"),
        ("", "small"),
        ("Paid by UPI. Thank you!", "small"),
    ],
    "receipt_stage_decor_2500.png": [
        ("FESTIVE DECOR HOUSE", "title"),
        ("44 Commercial Street, Bengaluru", "small"),
        ("", "small"),
        ("Invoice 2093         Date: 28-09-2026", "body"),
        ("----------------------------------------", "body"),
        ("Stage backdrop cloth           1400.00", "body"),
        ("LED string lights x 6           780.00", "body"),
        ("Balloons and tape               320.00", "body"),
        ("----------------------------------------", "body"),
        ("GRAND TOTAL                 Rs. 2500.00", "title"),
        ("", "small"),
        ("Goods once sold cannot be returned", "small"),
    ],
}

# A long, narrow till receipt with a quantity/price/amount table, a subtotal, several tax
# lines and a final total that differs from every item price. Saved small (420 px wide),
# like a phone screenshot, which is the case OCR used to get wrong.
NARROW_RECEIPTS = {
    "receipt_grocery_6_items_968.png": [
        ("NATURE BASKET STORES", "title"),
        ("14 Tapovan Road, Camp, Amravati 444602", "small"),
        ("PHONE : +911234567890", "small"),
        ("GSTIN : 27AAFCV2449G1Z7", "small"),
        ("", "small"),
        ("Bill No: IN-15          Date: 23-09-2026", "body"),
        ("----------------------------------------", "body"),
        ("SN Item            Qty   Price      Amt", "body"),
        ("----------------------------------------", "body"),
        ("1  Orange Powder    1   400.00   448.00", "body"),
        ("2  Walnuts          1   100.00   105.00", "body"),
        ("3  Silver Coin      1   100.00   103.00", "body"),
        ("4  Rose Water       1   150.00   150.00", "body"),
        ("5  Glycerine        1    50.00    50.00", "body"),
        ("6  Cheese           1   100.00   112.00", "body"),
        ("----------------------------------------", "body"),
        ("Subtotal            6            900.00", "body"),
        ("IGST at 3%                         3.00", "body"),
        ("IGST at 5%                         5.00", "body"),
        ("IGST at 12%                       60.00", "body"),
        ("----------------------------------------", "body"),
        ("TOTAL                        Rs. 968.00", "title"),
        ("", "small"),
        ("Cash tendered                   1000.00", "small"),
        ("Change                            32.00", "small"),
        ("Thank You", "small"),
    ],
}
NARROW_WIDTH = 420

SIZES = {"title": 34, "body": 28, "small": 24}


def _monospace(size: int) -> ImageFont.FreeTypeFont | None:
    """A fixed-width font so table columns line up; None if the machine has none of these."""
    for path in ("/System/Library/Fonts/Menlo.ttc", "/System/Library/Fonts/Monaco.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return None


def draw(lines: list[tuple[str, str]], *, table: bool = False) -> Image.Image:
    image = Image.new("RGB", (900, 120 + 52 * len(lines)), "white")
    pen = ImageDraw.Draw(image)
    y = 50
    for text, style in lines:
        font = (table and _monospace(SIZES[style] + 2)) or ImageFont.load_default(size=SIZES[style])
        pen.text((40 if table else 50, y), text, fill="black", font=font)
        y += 52
    return image


def draw_narrow(lines: list[tuple[str, str]]) -> Image.Image:
    full = draw(lines, table=True)
    return full.resize((NARROW_WIDTH, round(full.height * NARROW_WIDTH / full.width)), Image.LANCZOS)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, lines in RECEIPTS.items():
        draw(lines).save(OUT_DIR / name)
        print("wrote", OUT_DIR / name)
    for name, lines in NARROW_RECEIPTS.items():
        draw_narrow(lines).save(OUT_DIR / name)
        print("wrote", OUT_DIR / name)


if __name__ == "__main__":
    main()
