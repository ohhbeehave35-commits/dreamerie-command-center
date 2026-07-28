"""
Real outbound email for the Command Center, via Gmail SMTP + an app password.

Configure with two env vars:
    GMAIL_ADDRESS       - the sending Gmail address (e.g. vinny@ohhbeehave.com
                           if it's a Google Workspace address, or a gmail.com one)
    GMAIL_APP_PASSWORD  - a 16-character Gmail App Password (NOT the account
                           password -- generate one at myaccount.google.com/apppasswords,
                           requires 2-Step Verification to be on)

If they're not set, sending is simply "not connected" and the tool says so
instead of crashing -- same graceful-degrade pattern as crm.py.
"""

import os
import re
import smtplib
from email.mime.text import MIMEText

_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]{2,}$')

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


def send_email(to: str, subject: str, body: str, business: str = "") -> str:
    """Actually send one email via Gmail SMTP. Returns a short confirmation
    or explanation of why it couldn't send -- never raises.

    `business` is the active chat mode. When it names a brand, the From address
    comes from that brand's own identity (see brand_identity) and a brand with
    nothing set up REFUSES rather than falling back to the main account --
    a wrong-brand email to a client cannot be unsent.

    Passing no business keeps the old single-identity behaviour, which is what
    the public widget and any non-mode caller still want.
    """
    if not to or not _EMAIL_RE.match(to.strip()):
        return f"That doesn't look like a valid email address: {to!r}."

    alias_note = ""
    if business:
        from . import brand_identity
        ident = brand_identity.resolve_email(business)
        if not ident.get("ok"):
            return ident.get("error", "That business isn't set up to send email yet.")
        from_address, login, app_password = ident["from"], ident["login"], ident["password"]
        if ident["alias"] and from_address.lower() != login.lower():
            # Gmail only honours a different From if that address is verified
            # under Settings > Accounts > "Send mail as". If it is NOT verified
            # Google quietly rewrites From back to the account address and the
            # send still succeeds -- a wrong-brand email with no error anywhere.
            # We cannot detect that over SMTP, so say it out loud instead.
            alias_note = (
                f" Sent as {from_address}; if that address isn't verified under "
                f"Gmail > Settings > Accounts > 'Send mail as', Google will have "
                f"rewritten it to {login} -- worth checking the sent copy once."
            )
    else:
        from_address = login = get_gmail_address()
        app_password = get_gmail_app_password()
        if not (from_address and app_password):
            return "Email isn't connected yet -- connect it from the Settings panel."

    msg = MIMEText(body)
    msg["Subject"] = subject or "(no subject)"
    msg["From"] = from_address
    msg["To"] = to
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as s:
            s.login(login, app_password)
            s.sendmail(from_address, [to], msg.as_string())
        return f"Sent to {to} from {from_address}.{alias_note}"
    except Exception as e:
        return f"Couldn't send that email: {type(e).__name__}: {e}"
