"""
Twilio SMS integration for Stinger Industries Command Center.

Env vars required (set in Render):
    TWILIO_ACCOUNT_SID   -- Twilio account SID (starts with AC...)
    TWILIO_AUTH_TOKEN    -- Twilio auth token
    TWILIO_FROM_NUMBER   -- your Twilio phone number, e.g. +17725550100

Usage:
    from . import sms
    sms.send(to="+17725551234", body="Your appointment is confirmed!")

Called automatically by buildertrend.py when a milestone webhook fires.
"""

import os
import re
import httpx
import logging

log = logging.getLogger(__name__)

_TWILIO_BASE = "https://api.twilio.com/2010-04-01"


def is_configured() -> bool:
    return bool(
        os.environ.get("TWILIO_ACCOUNT_SID")
        and os.environ.get("TWILIO_AUTH_TOKEN")
        and os.environ.get("TWILIO_FROM_NUMBER")
    )


def _e164(raw: str) -> str:
    """Normalize a US phone number to E.164 format (+1XXXXXXXXXX)."""
    digits = re.sub(r'\D', '', raw)
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits[0] == '1':
        return f"+{digits}"
    raise ValueError(f"Cannot normalize to E.164: {raw!r}")


def send(to: str, body: str) -> dict:
    """
    Send an SMS via Twilio. Returns the Twilio message object dict.
    Raises httpx.HTTPStatusError on API failure.
    """
    if not is_configured():
        log.warning("SMS not configured (TWILIO_* env vars missing) — skipping send to %s", to)
        return {"status": "skipped", "reason": "Twilio not configured"}

    to = _e164(to)
    account_sid = os.environ["TWILIO_ACCOUNT_SID"]
    auth_token  = os.environ["TWILIO_AUTH_TOKEN"]
    from_number = os.environ["TWILIO_FROM_NUMBER"]

    resp = httpx.post(
        f"{_TWILIO_BASE}/Accounts/{account_sid}/Messages.json",
        auth=(account_sid, auth_token),
        data={"From": from_number, "To": to, "Body": body},
        timeout=15,
    )
    resp.raise_for_status()
    result = resp.json()
    log.info("SMS sent to %s — SID: %s", to, result.get("sid"))
    return result
