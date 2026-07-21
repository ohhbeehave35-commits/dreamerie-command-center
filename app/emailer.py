"""
Real outbound email for the Command Center, via Gmail SMTP + an app password.

Configure with two env vars:
    GMAIL_ADDRESS       - the sending Gmail address
    GMAIL_APP_PASSWORD  - a 16-character Gmail App Password (NOT the account
                           password -- generate one at myaccount.google.com/apppasswords,
                           requires 2-Step Verification to be on)

If they're not set, sending is simply "not connected" and the tool says so
instead of crashing -- same graceful-degrade pattern as crm.py.
"""

import os
import smtplib
from email.mime.text import MIMEText

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")


def is_configured() -> bool:
    return bool(GMAIL_ADDRESS and GMAIL_APP_PASSWORD)


def send_email(to: str, subject: str, body: str) -> str:
    """Actually send one email via Gmail SMTP. Returns a short confirmation
    or explanation of why it couldn't send -- never raises."""
    if not is_configured():
        return "Email isn't connected yet (GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set)."
    if not to or "@" not in to:
        return f"That doesn't look like a valid email address: {to!r}."
    msg = MIMEText(body)
    msg["Subject"] = subject or "(no subject)"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = to
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as s:
            s.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            s.sendmail(GMAIL_ADDRESS, [to], msg.as_string())
        return f"Sent to {to}."
    except Exception as e:
        return f"Couldn't send that email: {type(e).__name__}: {e}"
