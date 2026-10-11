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


# --- One look for every email -----------------------------------------------------------
# The site's colours, set inline on tables: mail apps ignore stylesheets and most modern CSS.
INK, CREAM, PAPER, BUTTER, CORAL, MINT, COBALT = "#080808", "#f4efdf", "#fbfaf7", "#f4f068", "#f04b37", "#8de8bd", "#2858bd"
HEAVY = "Impact, 'Anton', 'Arial Narrow Bold', 'Arial Black', Arial, sans-serif"   # the site's display face, where installed
SANS = "Inter, -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"


def _layout(*, club_name: str, tag: str, accent: str, accent_ink: str, heading: str, intro: str,
            rows: list[tuple[str, str]], qr_cid: str, qr_caption: str, button: str, url: str, footnote: str) -> str:
    """The HTML body shared by ticket and certificate emails. Every value is escaped here."""
    details = "".join(
        f'<tr><td style="padding:6px 16px 6px 0;font:600 11px {SANS};letter-spacing:1px;text-transform:uppercase;'
        f'color:#5b5b55;white-space:nowrap;vertical-align:top">{escape(label)}</td>'
        f'<td style="padding:6px 0;font:600 15px {SANS};color:{INK}">{escape(value)}</td></tr>'
        for label, value in rows
    )
    return f"""<!doctype html><html><body style="margin:0;padding:0;background:{INK}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{INK}"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="560" cellpadding="0" cellspacing="0" style="width:100%;max-width:560px;background:{CREAM};border:3px solid {INK};border-radius:22px;overflow:hidden">
  <tr><td style="background:{BUTTER};border-bottom:3px solid {INK};padding:14px 22px">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
      <td style="font:400 20px {HEAVY};letter-spacing:0.5px;text-transform:uppercase;color:{INK}">{escape(club_name)}</td>
      <td align="right"><span style="display:inline-block;padding:6px 12px;border:2px solid {INK};border-radius:999px;background:{accent};
        font:700 10px {SANS};letter-spacing:1.2px;text-transform:uppercase;color:{accent_ink}">{escape(tag)}</span></td>
    </tr></table></td></tr>
  <tr><td style="padding:28px 26px 8px">
    <h1 style="margin:0 0 10px;font:400 34px/1 {HEAVY};letter-spacing:0.5px;text-transform:uppercase;color:{INK}">{escape(heading)}</h1>
    <p style="margin:0 0 18px;font:400 15px/1.5 {SANS};color:#3a3a36">{intro}</p>
    <table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 0 20px">{details}</table>
  </td></tr>
  <tr><td align="center" style="padding:0 26px 8px">
    <table role="presentation" cellpadding="0" cellspacing="0" style="background:#ffffff;border:3px solid {INK};border-radius:16px"><tr>
      <td align="center" style="padding:14px"><img src="cid:{qr_cid}" alt="QR code" width="220" height="220" style="display:block;border:0"></td></tr></table>
    <p style="margin:10px 0 0;font:400 13px/1.45 {SANS};color:#3a3a36">{escape(qr_caption)}</p>
  </td></tr>
  <tr><td align="center" style="padding:18px 26px 26px">
    <a href="{escape(url)}" style="display:inline-block;padding:13px 26px;background:{INK};border-radius:999px;
      font:700 13px {SANS};letter-spacing:0.6px;text-transform:uppercase;color:{PAPER};text-decoration:none">{escape(button)}</a>
  </td></tr>
  <tr><td style="background:{PAPER};border-top:3px solid {INK};padding:14px 22px;font:400 12px/1.5 {SANS};color:#5b5b55">
    {escape(footnote)}<br>Sent by WhoDoo for {escape(club_name)}.</td></tr>
</table></td></tr></table></body></html>"""


def _message(*, to: str, subject: str, club_name: str, text: str, html: str, cid: str, qr_value: str, filename: str) -> EmailMessage:
    """A message with a plain-text part, the HTML part, and the QR code attached inline to the HTML."""
    settings = get_settings()
    sender = settings.mail_from or settings.smtp_user or "no-reply@whodoo.local"
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((f"{club_name} via WhoDoo", sender))
    message["To"] = to
    message["Message-ID"] = make_msgid(domain="whodoo.local")
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    message.get_payload()[1].add_related(qr_png(qr_value), "image", "png", cid=f"<{cid}>", filename=filename)
    return message


def ticket_email(
    *, to: str, full_name: str, participant_code: str, event_name: str, club_name: str, when: str,
    venue: str, credential: str, ticket_url: str, amount: str,
) -> EmailMessage:
    """The confirmation a participant receives: the details, and the QR ticket as an inline image."""
    cid = make_msgid(domain="whodoo.local")[1:-1]
    text = (
        f"Hi {full_name},\n\nYou are registered successfully for {event_name}.\n\n"
        f"When: {when}\nWhere: {venue}\nParticipant ID: {participant_code}\nPaid: {amount}\n\n"
        f"Your QR ticket: {ticket_url}\n\n"
        "Show the QR code at the entrance along with your college ID. It admits one person, once, "
        "so do not share it.\n\n" + club_name + "\n"
    )
    html = _layout(
        club_name=club_name, tag="Ticket", accent=MINT, accent_ink=INK, heading="You're in.",
        intro=f"Hi {escape(full_name)}, you are registered successfully for <strong>{escape(event_name)}</strong>. Here is your ticket.",
        rows=[("Event", event_name), ("When", when), ("Where", venue), ("Participant ID", participant_code), ("Paid", amount)],
        qr_cid=cid, qr_caption="Show this code at the entrance with your college ID. It admits one person, once: do not share it.",
        button="Open your ticket", url=ticket_url,
        footnote="Lost this email? Your ticket is always at the link above.",
    )
    return _message(to=to, subject=f"Your ticket for {event_name} ({participant_code})", club_name=club_name,
                    text=text, html=html, cid=cid, qr_value=credential, filename="ticket-qr.png")


CERTIFICATE_ACCENTS = {"participation": (COBALT, "#ffffff"), "volunteering": (CORAL, INK), "achievement": (MINT, INK)}


def certificate_email(
    *, to: str, recipient_name: str, title: str, kind: str, club_name: str, issued_by: str, issued_on: str,
    verify_code: str, verify_url: str, page_url: str,
) -> EmailMessage:
    """Tells someone a certificate has been issued to them. The QR code is the public check for it."""
    cid = make_msgid(domain="whodoo.local")[1:-1]
    spaced = " ".join(verify_code[i:i + 4] for i in range(0, len(verify_code), 4))
    label = kind.replace("_", " ").capitalize()
    text = (
        f"Hi {recipient_name},\n\n{club_name} has issued you a certificate of {label.lower()}: {title}.\n\n"
        f"Issued by: {issued_by}\nIssued on: {issued_on}\nVerification code: {spaced}\n\n"
        f"Open and print it: {page_url}\nAnyone can check it is genuine at: {verify_url}\n\n{club_name}\n"
    )
    accent, accent_ink = CERTIFICATE_ACCENTS.get(kind, (COBALT, "#ffffff"))
    html = _layout(
        club_name=club_name, tag=label, accent=accent, accent_ink=accent_ink, heading="Your certificate",
        intro=f"Hi {escape(recipient_name)}, {escape(club_name)} has issued you a certificate of {escape(label.lower())}: "
              f"<strong>{escape(title)}</strong>.",
        rows=[("Awarded to", recipient_name), ("For", title), ("Issued by", issued_by), ("Issued on", issued_on), ("Code", spaced)],
        qr_cid=cid, qr_caption="Anyone can scan this code, or enter the code above, to check the certificate is genuine.",
        button="Open and print", url=page_url,
        footnote="Opening the certificate needs your WhoDoo sign-in. The check by code needs none.",
    )
    return _message(to=to, subject=f"Your certificate: {title}", club_name=club_name, text=text, html=html, cid=cid,
                    qr_value=verify_url, filename="certificate-qr.png")


def notice_email(*, to: str, full_name: str, club_name: str, subject: str, heading: str, intro: str,
                 rows: list[tuple[str, str]], button: str, url: str) -> EmailMessage:
    """A short update about an event someone registered for, in the same colours as the ticket email."""
    settings = get_settings()
    sender = settings.mail_from or settings.smtp_user or "no-reply@whodoo.local"
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((f"{club_name} via WhoDoo", sender))
    message["To"] = to
    message["Message-ID"] = make_msgid(domain="whodoo.local")
    message.set_content(f"Hi {full_name},\n\n{intro}\n\n" + "\n".join(f"{label}: {value}" for label, value in rows)
                        + f"\n\n{button}: {url}\n\n{club_name}\n")
    details = "".join(
        f'<tr><td style="padding:6px 16px 6px 0;font:600 11px {SANS};letter-spacing:1px;text-transform:uppercase;color:#5b5b55">{escape(label)}</td>'
        f'<td style="padding:6px 0;font:600 15px {SANS};color:{INK}">{escape(value)}</td></tr>' for label, value in rows)
    message.add_alternative(f"""<!doctype html><html><body style="margin:0;padding:0;background:{INK}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{INK}"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="560" cellpadding="0" cellspacing="0" style="width:100%;max-width:560px;background:{CREAM};border:3px solid {INK};border-radius:22px;overflow:hidden">
  <tr><td style="background:{BUTTER};border-bottom:3px solid {INK};padding:14px 22px;font:400 20px {HEAVY};text-transform:uppercase;color:{INK}">{escape(club_name)}</td></tr>
  <tr><td style="padding:28px 26px 10px">
    <h1 style="margin:0 0 10px;font:400 32px/1 {HEAVY};text-transform:uppercase;color:{INK}">{escape(heading)}</h1>
    <p style="margin:0 0 18px;font:400 15px/1.5 {SANS};color:#3a3a36">Hi {escape(full_name)}, {escape(intro)}</p>
    <table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 0 20px">{details}</table>
  </td></tr>
  <tr><td align="center" style="padding:0 26px 26px"><a href="{escape(url)}" style="display:inline-block;padding:13px 26px;background:{INK};border-radius:999px;font:700 13px {SANS};letter-spacing:0.6px;text-transform:uppercase;color:{PAPER};text-decoration:none">{escape(button)}</a></td></tr>
  <tr><td style="background:{PAPER};border-top:3px solid {INK};padding:14px 22px;font:400 12px {SANS};color:#5b5b55">Your ticket and its QR code are unchanged. Sent by WhoDoo for {escape(club_name)}.</td></tr>
</table></td></tr></table></body></html>""", subtype="html")
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
