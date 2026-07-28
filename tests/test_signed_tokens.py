"""Purpose-separated tokens: prove cross-purpose replay is impossible.

The whole reason this module exists is that users.verify_session_token accepts
any "x:int:hmac" as a session. These tests assert that a token minted here for
one purpose CANNOT be used for another, and -- the load-bearing one -- cannot be
replayed as a session cookie.
"""

import time

import pytest

from app import signed_tokens as st
from app import users


@pytest.fixture(autouse=True)
def fixed_secret(monkeypatch):
    monkeypatch.setattr(users, "get_session_secret", lambda: b"x" * 32)


def test_roundtrip_returns_payload():
    tok = st.mint("webauthn-auth", {"user": "susan"}, ttl_seconds=60)
    body = st.verify("webauthn-auth", tok)
    assert body is not None and body["user"] == "susan"


def test_a_token_for_one_purpose_fails_for_another():
    tok = st.mint("webauthn-reg", {"user": "susan"}, ttl_seconds=60)
    assert st.verify("webauthn-auth", tok) is None
    assert st.verify("password-reset", tok) is None


def test_challenge_cannot_be_replayed_as_a_session(monkeypatch):
    """The landmine, directly. Mint a challenge, hand it to the SESSION verifier
    and confirm it is not accepted as a login for anyone."""
    tok = st.mint("webauthn-auth", {"user": "susan"}, ttl_seconds=60)
    # users.verify_session_token uses its own colon format; a dotted token from
    # here must simply not verify there.
    assert users.verify_session_token(tok) is None


def test_session_token_is_not_accepted_here():
    """And the reverse: a real session token is not a valid signed_token of any
    purpose."""
    sess = users.create_session_token("susan")
    assert st.verify("webauthn-auth", sess) is None
    assert st.verify("session", sess) is None


def test_expired_token_is_rejected(monkeypatch):
    tok = st.mint("webauthn-auth", {"user": "susan"}, ttl_seconds=1)
    monkeypatch.setattr(st.time, "time", lambda: time.time() + 5)
    assert st.verify("webauthn-auth", tok) is None


def test_tampered_payload_is_rejected():
    tok = st.mint("webauthn-auth", {"user": "susan"}, ttl_seconds=60)
    raw_b64, sig_b64 = tok.split(".")
    forged = st._b64e(b'{"user":"attacker","_iat":0,"_exp":9999999999}')
    assert st.verify("webauthn-auth", forged + "." + sig_b64) is None


def test_future_dated_token_is_rejected(monkeypatch):
    """A token minted 'later' than now-plus-slack is tampering, not drift."""
    real = st.time.time
    monkeypatch.setattr(st.time, "time", lambda: real() + 10000)
    tok = st.mint("webauthn-auth", {"user": "susan"}, ttl_seconds=60)
    monkeypatch.setattr(st.time, "time", real)
    assert st.verify("webauthn-auth", tok) is None


def test_missing_expiry_is_rejected():
    """A hand-built token with no _exp must not be treated as non-expiring."""
    import hmac, hashlib, json
    raw = json.dumps({"user": "susan"}, separators=(",", ":"), sort_keys=True).encode()
    key = hmac.new(b"x" * 32, st._VERSION + b"webauthn-auth", hashlib.sha256).digest()
    sig = hmac.new(key, raw, hashlib.sha256).digest()
    tok = st._b64e(raw) + "." + st._b64e(sig)
    assert st.verify("webauthn-auth", tok) is None


def test_garbage_is_rejected_not_raised():
    for junk in ("", "no-dot", "a.b.c", "!!!.???", "."):
        assert st.verify("webauthn-auth", junk) is None


def test_purpose_with_colon_is_refused():
    """A colon in the purpose could let two purposes collide after derivation."""
    with pytest.raises(ValueError):
        st.mint("a:b", {"x": 1}, ttl_seconds=60)


def test_bumping_version_invalidates_everything(monkeypatch):
    tok = st.mint("webauthn-auth", {"user": "susan"}, ttl_seconds=60)
    monkeypatch.setattr(st, "_VERSION", b"cc/v2/")
    assert st.verify("webauthn-auth", tok) is None
