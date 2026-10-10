"""Send one sample ticket email, to check the mail settings in .env before a demo.

    python -m scripts.send_test_email you@example.com

Says plainly whether the mail server accepted it, and why not if it did not.
"""

import sys

from app.core.config import get_settings
from app.services import email_service, gatesync_service


def main() -> None:
    if len(sys.argv) != 2 or "@" not in sys.argv[1]:
        raise SystemExit("Usage: python -m scripts.send_test_email you@example.com")
    settings = get_settings()
    if not email_service.configured():
        raise SystemExit("Not sent: SMTP_HOST is empty in .env. For Gmail set SMTP_HOST=smtp.gmail.com, "
                         "SMTP_PORT=587, SMTP_USER=<your address> and SMTP_PASSWORD=<an app password>.")
    import uuid

    message = email_service.ticket_email(
        to=sys.argv[1], full_name="Test Student", participant_code="WD-TEST01", event_name="WhoDoo email test",
        club_name="WhoDoo", when="Today", venue="Not a real event",
        credential=gatesync_service.credential_for(uuid.uuid4()),   # belongs to no registration: admits nobody
        ticket_url=settings.app_base_url, amount="Free",
    )
    outcome = email_service.deliver(message)
    if outcome == "sent":
        print(f"Sent to {sys.argv[1]} through {settings.smtp_host}. Check the inbox (and spam).")
    else:
        raise SystemExit(f"Not sent: {email_service.last_failure}.")


if __name__ == "__main__":
    main()
