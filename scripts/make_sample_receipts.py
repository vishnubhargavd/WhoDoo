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

SIZES = {"title": 34, "body": 28, "small": 24}


def draw(lines: list[tuple[str, str]]) -> Image.Image:
    image = Image.new("RGB", (900, 120 + 52 * len(lines)), "white")
    pen = ImageDraw.Draw(image)
    y = 50
    for text, style in lines:
        pen.text((50, y), text, fill="black", font=ImageFont.load_default(size=SIZES[style]))
        y += 52
    return image


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, lines in RECEIPTS.items():
        draw(lines).save(OUT_DIR / name)
        print("wrote", OUT_DIR / name)


if __name__ == "__main__":
    main()
