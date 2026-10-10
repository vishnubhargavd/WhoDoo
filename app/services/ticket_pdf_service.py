"""Ticket PDF generation service using Pillow.

Creates a branded event ticket with event details, seat, venue,
pricing status (FREE or Paid), and verification information.
"""

import io
from PIL import Image, ImageDraw, ImageFont


def generate_ticket_pdf(
    event_name: str,
    venue: str | None,
    time_str: str,
    holder_name: str,
    seat_number: int,
    ticket_id: str,
    credential: str,
    is_free: bool = True,
) -> bytes:
    width, height = 650, 900
    img = Image.new("RGB", (width, height), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)

    # Top Brand Header Banner (Dark Ink)
    draw.rectangle([0, 0, width, 140], fill=(13, 27, 42))

    # Header text
    draw.text((40, 35), "WHODOO  ·  GATESYNC", fill=(141, 232, 189))
    draw.text((40, 70), "Official Digital Event Pass", fill=(255, 255, 255))

    # Badge: FREE or CONFIRMED
    badge_text = "CONFIRMED FREE PASS" if is_free else "CONFIRMED ADMISSION"
    draw.rounded_rectangle([40, 165, 300, 205], radius=6, fill=(240, 253, 244), outline=(134, 239, 172), width=1)
    draw.text((55, 177), badge_text, fill=(22, 101, 52))

    # Event Name
    draw.text((40, 230), event_name, fill=(15, 23, 42))

    # Decorative separator
    draw.line([40, 290, width - 40, 290], fill=(226, 232, 240), width=1)

    # Event Details Section
    y = 315
    draw.text((40, y), "DATE & TIME (IST)", fill=(100, 116, 139))
    draw.text((40, y + 25), time_str, fill=(15, 23, 42))

    draw.text((340, y), "CAMPUS VENUE", fill=(100, 116, 139))
    draw.text((340, y + 25), venue or "Campus Hall (TBA)", fill=(15, 23, 42))

    y += 90
    draw.text((40, y), "TICKET HOLDER", fill=(100, 116, 139))
    draw.text((40, y + 25), holder_name, fill=(15, 23, 42))

    draw.text((340, y), "SEAT NUMBER", fill=(100, 116, 139))
    draw.text((340, y + 25), f"Seat #{seat_number}", fill=(15, 23, 42))

    # QR Container Box
    y += 110
    draw.rounded_rectangle([40, y, width - 40, y + 220], radius=12, fill=(248, 250, 252), outline=(203, 213, 225), width=1)

    draw.text((65, y + 20), "GATESYNC CHECK-IN CREDENTIAL", fill=(71, 85, 105))
    draw.text((65, y + 50), "Opaque Bearer Token:", fill=(100, 116, 139))

    # Truncated or wrapped token box
    token_display = credential if len(credential) <= 40 else (credential[:38] + "...")
    draw.rectangle([65, y + 80, width - 65, y + 130], fill=(255, 255, 255), outline=(226, 232, 240))
    draw.text((80, y + 98), token_display, fill=(30, 41, 59))

    draw.text((65, y + 155), "Present this pass or scan in the WhoDoo mobile check-in wallet.", fill=(100, 116, 139))
    draw.text((65, y + 180), f"Ticket ID: {ticket_id}", fill=(148, 163, 184))

    # Footer
    draw.line([40, height - 70, width - 40, height - 70], fill=(241, 245, 249), width=1)
    draw.text((40, height - 50), "WhoDoo · Student Organization Platform · All Rights Reserved", fill=(148, 163, 184))

    buffer = io.BytesIO()
    img.save(buffer, format="PDF", resolution=150.0)
    return buffer.getvalue()
