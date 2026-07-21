"""
Real outbound email for the Command Center, via Gmail SMTP + an app password.

Configure via the Settings panel in the dashboard (preferred -- takes effect
immediately, no redeploy), or with two env vars as a fallback:
    GMAIL_ADDRESS       - the sending Gmail address
    GMAIL_APP_PASSWORD  - a 16-character Gmail App Password (NOT the account
                           password -- generate one at myaccount.google.com/apppasswords,
                           requires 2-Step Verification to be on)

If neither is set, sending is simply "not connected" and the tool says so
instead of crashing -- same graceful-degrade pattern as crm.py.
"""

import os
import smtplib
from email.mime.text import MIMEText

from . import crm

# Settings-page values (Airtable, editable live from the dashboard) win over
# env vars (Render, requires a redeploy) -- lets the owner self-serve connect
# email without ever touching Render.
GMAIL_ADDRESS_KEY = "gmail_address"
GMAIL_APP_PASSWORD_KEY = "gmail_app_password"


def get_gmail_address() -> str:
    return crm.get_setting(GMAIL_ADDRESS_KEY, "") or os.environ.get("GMAIL_ADDRESS", "")


def get_gmail_app_password() -> str:
    return crm.get_setting(GMAIL_APP_PASSWORD_KEY, "") or os.environ.get("GMAIL_APP_PASSWORD", "")


def is_configured() -> bool:
    return bool(get_gmail_address() and get_gmail_app_password())


def send_email(to: str, subject: str, body: str) -> str:
    """Actually send one email via Gmail SMTP. Returns a short confirmation
    or explanation of why it couldn't send -- never raises."""
    address, app_password = get_gmail_address(), get_gmail_app_password()
    if not (address and app_password):
        return "Email isn't connected yet -- connect it from the Settings panel."
    if not to or "@" not in to:
        return f"That doesn't look like a valid email address: {to!r}."
    msg = MIMEText(body)
    msg["Subject"] = subject or "(no subject)"
    msg["From"] = address
    msg["To"] = to
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as s:
            s.login(address, app_password)
            s.sendmail(address, [to], msg.as_string())
        return f"Sent to {to}."
    except Exception as e:
        return f"Couldn't send that email: {type(e).__name__}: {e}"
