"""Ticket emails.

With SMTP_HOST set (for example smtp.gmail.com with an app password) the message is sent.
Without it, or if sending fails, the message is written to MAIL_OUTBOX_DIR as an .eml file,
so a registration never depends on the mail server being reachable.
"""

import io
import logging
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from html import escape
from pathlib import Path

import segno

from app.core.config import get_settings

logger = logging.getLogger("whodoo.mail")

# Why the last attempt failed, in words safe to show an organiser. Never a password or an address.
last_failure: str | None = None


def _why(exc: Exception) -> str:
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "the mail server refused the sign-in: check SMTP_USER and that SMTP_PASSWORD is an app password"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "the mail server refused the recipient's address"
    if isinstance(exc, smtplib.SMTPException):
        return f"the mail server rejected the message ({type(exc).__name__})"
    return f"the mail server could not be reached ({type(exc).__name__}): check SMTP_HOST, SMTP_PORT and the network"


def qr_png(value: str) -> bytes:
    buffer = io.BytesIO()
    segno.make(value, error="m").save(buffer, kind="png", scale=8, border=3)
    return buffer.getvalue()


def ticket_email(
    *, to: str, full_name: str, participant_code: str, event_name: str, club_name: str, when: str,
    venue: str, credential: str, ticket_url: str, amount: str,
) -> EmailMessage:
    """The confirmation a participant receives: details in text, the QR ticket as an inline image."""
    settings = get_settings()
    sender = settings.mail_from or settings.smtp_user or "no-reply@whodoo.local"
    message = EmailMessage()
    message["Subject"] = f"Your ticket for {event_name} ({participant_code})"
    message["From"] = formataddr((f"{club_name} via WhoDoo", sender))
    message["To"] = to
    message["Message-ID"] = make_msgid(domain="whodoo.local")
    message.set_content(
        f"Hi {full_name},\n\nYou are registered successfully for {event_name}.\n\n"
        f"When: {when}\nWhere: {venue}\nParticipant ID: {participant_code}\nPaid: {amount}\n\n"
        f"Your QR ticket: {ticket_url}\n\n"
        "Show the QR code at the entrance along with your college ID. It admits one person, once, "
        "so do not share it.\n\n" + club_name + "\n"
    )
    cid = make_msgid(domain="whodoo.local")
    rows = "".join(
        f'<tr><td style="padding:4px 16px 4px 0;color:#666">{escape(label)}</td>'
        f'<td style="padding:4px 0"><strong>{escape(value)}</strong></td></tr>'
        for label, value in (("When", when), ("Where", venue), ("Participant ID", participant_code), ("Paid", amount))
    )
    message.add_alternative(
        f"""<div style="font-family:Arial,Helvetica,sans-serif;max-width:520px;margin:auto;color:#111">
<h2 style="margin:0 0 4px">You are registered successfully</h2>
<p style="margin:0 0 16px;color:#444">Hi {escape(full_name)}, here is your ticket for <strong>{escape(event_name)}</strong>.</p>
<table style="border-collapse:collapse;margin-bottom:16px">{rows}</table>
<div style="border:2px solid #111;border-radius:16px;padding:16px;text-align:center">
<img src="cid:{cid[1:-1]}" alt="QR ticket" width="240" height="240" style="display:block;margin:auto">
<p style="margin:8px 0 0;font-size:13px;color:#444">Show this at the entrance with your college ID.<br>
It admits one person, once. Do not share it.</p></div>
<p style="font-size:13px"><a href="{escape(ticket_url)}">Open your ticket online</a></p>
<p style="font-size:12px;color:#777">{escape(club_name)} · sent by WhoDoo</p></div>""",
        subtype="html",
    )
    message.get_payload()[1].add_related(qr_png(credential), "image", "png", cid=cid, filename="ticket-qr.png")
    return message


def _to_outbox(message: EmailMessage) -> Path:
    folder = Path(get_settings().mail_outbox_dir)
    folder.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() else "_" for ch in str(message["To"]))[:60]
    path = folder / f"{datetime.now():%Y%m%d-%H%M%S-%f}-{safe}.eml"
    path.write_bytes(bytes(message))
    return path


def configured() -> bool:
    return bool(get_settings().smtp_host)


def deliver(message: EmailMessage) -> str:
    """Send the message. Blocking. Returns what really happened:

    sent    the mail server accepted it
    saved   no mail server is set up; written to the outbox and not sent to anyone
    failed  the mail server refused it or could not be reached; kept in the outbox
    """
    global last_failure
    settings = get_settings()
    outcome = "saved"
    if settings.smtp_host:
        outcome = "failed"
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as server:
                server.starttls(context=ssl.create_default_context())
                if settings.smtp_user:
                    # Google shows app passwords in groups of four; the spaces are not part of it.
                    server.login(settings.smtp_user, settings.smtp_password.replace(" ", ""))
                server.send_message(message)
            last_failure = None
            return "sent"
        except (OSError, smtplib.SMTPException) as exc:
            last_failure = _why(exc)
            logger.error("Ticket email not sent: %s. Kept in the outbox.", last_failure)
    _to_outbox(message)
    return outcome
