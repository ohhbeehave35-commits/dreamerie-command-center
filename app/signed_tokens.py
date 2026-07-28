"""Purpose-separated signed tokens.

There is a live landmine in users.verify_session_token: the payload it signs is
just "username:issued_at", and the verifier accepts ANY value shaped
"<something>:<int>:<valid-hmac>", handing <something> back as an authenticated
username. Nothing in the token says what it is FOR. The only reason a
differently-shaped token is rejected today is that the verifier happens to
require exactly two colon-separated fields -- a parsing side effect, not a
security control, and one that any future refactor silently removes.

That matters the moment a second kind of token exists. A WebAuthn challenge, a
password-reset link, an email-confirmation token -- sign any of them with the
same key and the same shape and they become session cookies. Paste one into
cc_session and you are logged in as whatever string is in field one.

So every new token goes through here, and here derives a SEPARATE KEY PER
PURPOSE from the one root secret:

    key(purpose) = HMAC(root_secret, b"cc/v1/" + purpose)

A token minted for "webauthn-reg" cannot verify under the key for
"webauthn-auth" or for anything else, regardless of its shape, and none of them
can verify as a session token. This is key separation, not a convention -- it
cannot be defeated by a parser change, a refactor, or someone adding a field.

Expiry is INSIDE the signed payload rather than hardcoded in the verifier, so a
60-second challenge and a 30-day session can share this machinery without the
verifier needing to know which is which.
"""

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Optional

from . import users

# Bumping this invalidates every outstanding token of every purpose at once.
# Cheap, blunt, and occasionally exactly what you want.
_VERSION = b"cc/v1/"


def _key_for(purpose: str) -> bytes:
    """A distinct signing key per purpose, derived from the one root secret.

    Deriving rather than sharing is the whole point: it makes cross-purpose
    replay impossible by construction instead of by careful payload parsing.
    """
    if not purpose or ":" in purpose:
        raise ValueError("purpose must be non-empty and contain no colon")
    return hmac.new(users.get_session_secret(), _VERSION + purpose.encode(),
                    hashlib.sha256).digest()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(txt: str) -> bytes:
    return base64.urlsafe_b64decode(txt + "=" * (-len(txt) % 4))


def mint(purpose: str, payload: dict, ttl_seconds: int) -> str:
    """Sign a payload for one purpose, expiring in ttl_seconds.

    The expiry travels INSIDE the signature, so it cannot be extended by
    editing the token, and the verifier does not need a per-purpose lifetime
    baked into it.
    """
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    body = dict(payload)
    now = int(time.time())
    body["_iat"] = now
    body["_exp"] = now + int(ttl_seconds)
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    sig = hmac.new(_key_for(purpose), raw, hashlib.sha256).digest()
    return _b64e(raw) + "." + _b64e(sig)


def verify(purpose: str, token: str) -> Optional[dict]:
    """Return the payload, or None. Never raises, never partially trusts.

    Fails CLOSED on anything unexpected: bad shape, bad signature, unparseable
    or missing expiry, expired, or a timestamp far enough in the future to
    suggest tampering. A malformed token is not a special case to be recovered
    from -- it is a rejection.
    """
    try:
        if not token or token.count(".") != 1:
            return None
        raw_b64, sig_b64 = token.split(".")
        raw, sig = _b64d(raw_b64), _b64d(sig_b64)

        expected = hmac.new(_key_for(purpose), raw, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None

        body = json.loads(raw)
        if not isinstance(body, dict):
            return None

        # Expiry must be present and numeric. A token whose expiry we cannot
        # read is not a token we honour -- treating it as non-expiring is how
        # short-lived credentials quietly become permanent ones.
        exp, iat = body.get("_exp"), body.get("_iat")
        if not isinstance(exp, int) or not isinstance(iat, int):
            return None
        now = int(time.time())
        if now >= exp:
            return None
        # Clocks drift; forged timestamps do not drift 5 minutes into the
        # future. Reject rather than silently accept a token minted "later".
        if iat > now + 300:
            return None
        return body
    except Exception:
        return None


def payload_field(purpose: str, token: str, field: str) -> Optional[Any]:
    """Convenience: verify, then read one field. None if either step fails."""
    body = verify(purpose, token)
    return None if body is None else body.get(field)
