"""Single-use, expiring set-password links (invite and reset).

A link carries a random token. Only the SHA-256 of the token is stored (in the
Settings table, key "invite:<hash>"), so a copy of the database cannot be turned
back into a working link. The link works for 24 hours and once: it is burned
the moment it is used, before the password is written.

The person who follows the link chooses their OWN password on the set-password
page; nobody else ever sees it. The raw token exists only inside the emailed
link. It is never returned to the chat model, logged, or printed.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import Optional, Tuple

from . import crm, users

TTL_SECONDS = 24 * 60 * 60
_PREFIX = "invite:"
# Process-local belt and braces: a token burned in this process stays burned even
# if the settings write is slow to land.
_burned: set = set()


def _hash(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


def create(username: str, purpose: str, now: Optional[float] = None) -> str:
    """Store a fresh link record and return the raw token (for the email only)."""
    if purpose not in ("invite", "reset"):
        raise ValueError("purpose must be invite or reset")
    token = secrets.token_urlsafe(32)
    t = time.time() if now is None else now
    rec = {"u": username, "p": purpose, "exp": int(t + TTL_SECONDS)}
    if not crm.set_setting(_PREFIX + _hash(token), json.dumps(rec), sync=True):
        raise RuntimeError("could not store the link")
    return token


def revoke(token: str) -> None:
    """Burn a token (used when the email could not be sent)."""
    h = _hash(token)
    _burned.add(h)
    crm.set_setting(_PREFIX + h, "", sync=True)


def _peek(token: str, now: Optional[float]) -> Tuple[Optional[dict], str]:
    if not token or len(token) < 20:
        return None, "This link is not valid."
    h = _hash(token)
    if h in _burned:
        return None, "This link has already been used. Ask for a new one."
    raw = crm.get_setting(_PREFIX + h, "")
    if not raw:
        return None, "This link is not valid or has already been used. Ask for a new one."
    try:
        rec = json.loads(raw)
    except Exception:
        return None, "This link is not valid."
    t = time.time() if now is None else now
    if t > float(rec.get("exp", 0)):
        crm.set_setting(_PREFIX + h, "", sync=True)
        return None, "This link has expired. Ask for a new one."
    return rec, ""


def check(token: str, now: Optional[float] = None) -> Tuple[bool, str]:
    """Is this token usable right now? Burns nothing."""
    rec, why = _peek(token, now)
    return rec is not None, why


def consume(token: str, new_password: str, now: Optional[float] = None) -> Tuple[bool, str]:
    """Burn the token, then set the person's own password. Returns (ok, message)."""
    rec, why = _peek(token, now)
    if rec is None:
        return False, why
    ok, reason = users.validate_password(new_password or "")
    if not ok:
        return False, reason  # not burned: they can try again with a better password
    h = _hash(token)
    _burned.add(h)
    crm.set_setting(_PREFIX + h, "", sync=True)
    if not users.update_user_password(rec["u"], new_password):
        return False, "We could not save the password. Ask for a new link."
    return True, "Password set. You can sign in now."
