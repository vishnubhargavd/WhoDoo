"""Transactional email delivery service for WhoDoo tickets and announcements.

Supports configured SMTP / transactional delivery with safe fallbacks and local
audited outbox logging during development. Never pretends an email was delivered
when external credentials are not configured.
"""

import logging
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from app.core.config import get_settings

logger = logging.getLogger("whodoo.email")


class EmailDeliveryResult:
    def __init__(self, success: bool, mode: str, detail: str):
        self.success = success
        self.mode = mode
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        return {"success": self.success, "mode": self.mode, "detail": self.detail}


def send_ticket_email(
    to_email: str,
    recipient_name: str,
    event_name: str,
    venue: str | None,
    starts_at_str: str,
    credential: str,
    ticket_id: str,
) -> EmailDeliveryResult:
    """Send ticket confirmation and QR access details to an authorized ticket holder."""
    smtp_host = os.environ.get("SMTP_HOST")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")
    sender = os.environ.get("SMTP_FROM", "tickets@whodoo.org")

    subject = f"Your WhoDoo Ticket: {event_name}"
    body = f"""Hi {recipient_name},

Your ticket for {event_name} is confirmed!

Event: {event_name}
Date & Time: {starts_at_str}
Venue: {venue or 'Campus Venue (TBA)'}
Ticket ID: {ticket_id}
QR Credential: {credential}

Present your digital QR credential at the GateSync checkpoint for seamless check-in.

See you there!
The WhoDoo Team
"""

    if smtp_host and smtp_user and smtp_pass:
        try:
            msg = EmailMessage()
            msg.set_content(body)
            msg["Subject"] = subject
            msg["From"] = sender
            msg["To"] = to_email

            with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
                server.starttls()
                server.login(smtp_user, smtp_pass)
                server.send_message(msg)
            logger.info("Ticket email delivered via SMTP to %s", to_email)
            return EmailDeliveryResult(True, "smtp", f"Delivered to {to_email}")
        except Exception as e:
            logger.warning("SMTP delivery failed for %s: %s", to_email, e)
            return EmailDeliveryResult(False, "smtp_error", str(e))

    # Honest local development outbox
    log_dir = Path(get_settings().upload_dir) / "outbox"
    log_dir.mkdir(parents=True, exist_ok=True)
    outbox_file = log_dir / "emails.log"
    entry = f"[{starts_at_str}] TO: {to_email} | SUBJECT: {subject} | TICKET: {ticket_id}\n{body}\n{'-'*60}\n"
    with open(outbox_file, "a", encoding="utf-8") as f:
        f.write(entry)

    logger.info("Transactional email provider not configured; queued in local outbox for %s", to_email)
    return EmailDeliveryResult(
        True,
        "local_outbox",
        f"Queued in local outbox ({outbox_file.name}) — transactional SMTP credentials not configured",
    )
