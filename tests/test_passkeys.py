"""Passkey sign-in: the properties that keep it from ever locking Susan out.

The order of these tests mirrors the risk list:
  1. The three lock-page endpoints are gate-EXEMPT (the /api/unlock lesson --
     gating the door's own button is how everyone got locked out).
  2. The enrollment endpoints are gate-PROTECTED (a stranger must never be
     able to register a fingerprint).
  3. Cross-purpose tokens are rejected (a registration challenge is not an
     authentication, and neither is a session).
  4. A used challenge cannot be replayed.
  5. A store outage is an honest 503 that never counts as a failed attempt.
  6. A verified assertion mints the SAME cc_access cookie /api/unlock mints.
"""

import pytest
from fastapi.testclient import TestClient

import app.main as m
import app.passkeys as pk
import app.signed_tokens as st
import app.support as support
import app.users as users
from app.main import app


CODE = "correct-horse-battery-staple-42"
HTTPS = "https://testserver"
RP = "testserver"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    support.reset_for_tests()
    pk.reset_for_tests()
    m._unlock_attempts.clear()
    m._used_passkey_challenges.clear()
    monkeypatch.setattr(users, "get_session_secret", lambda: b"x" * 32)
    yield
    support.reset_for_tests()
    pk.reset_for_tests()
    m._unlock_attempts.clear()
    m._used_passkey_challenges.clear()


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setattr(m, "ACCESS_CODE", "")
    monkeypatch.setattr(m.crm, "is_configured", lambda: True)
    monkeypatch.setattr(m.crm, "get_setting",
                        lambda k, d="": CODE if k == "access_code_override" else d)


def _stored_cred():
    return {"record_id": "recX", "CredentialID": "credid1", "PublicKey": "cHVibGlja2V5",
            "SignCount": "0", "Label": "Phone", "RpID": RP}


# ---- 1. the lock-page endpoints are exempt ---------------------------------

def test_lock_page_passkey_endpoints_are_not_swallowed_by_the_gate(gated, monkeypatch):
    """No cookies at all -> these must reach their handlers, not the gate's
    401 {"detail": "locked"}. This is the regression that bricked /api/unlock."""
    monkeypatch.setattr(pk, "list_credentials", lambda rp_id=None: [])
    monkeypatch.setattr(pk, "enabled_cached", lambda rp_id: False)
    c = TestClient(app, base_url=HTTPS)

    r = c.get("/api/passkey/enabled")
    assert r.status_code == 200 and r.json() == {"enabled": False}

    r = c.post("/api/passkey/auth-options")
    assert r.json().get("detail") != "locked", "gate swallowed auth-options"
    assert r.status_code == 404  # no credentials -> honest message, but ROUTED

    r = c.post("/api/passkey/auth", json={"state": "x", "credential": {}})
    assert r.json().get("detail") != "locked", "gate swallowed auth"
    assert r.status_code == 401  # bad state, but it reached the handler


# ---- 2. enrollment is protected --------------------------------------------

def test_strangers_cannot_reach_enrollment(gated):
    c = TestClient(app, base_url=HTTPS)  # no cookies
    assert c.post("/api/passkey/register-options").status_code == 401  # gate
    assert c.post("/api/passkey/register",
                  json={"state": "x", "credential": {}}).status_code == 401  # gate
    assert c.get("/api/passkey/credentials").status_code == 401  # gate


def test_unlocked_owner_can_reach_enrollment(gated, monkeypatch):
    monkeypatch.setattr(pk, "list_credentials", lambda rp_id=None: [])
    monkeypatch.setattr(pk, "get_user_handle", lambda: b"h" * 16)
    c = TestClient(app, base_url=HTTPS)
    assert c.post("/api/unlock", json={"code": CODE}).status_code == 200
    r = c.post("/api/passkey/register-options")
    assert r.status_code == 200
    j = r.json()
    assert "options" in j and "state" in j
    assert j["options"]["rp"]["id"] == RP


# ---- 3. cross-purpose tokens are rejected -----------------------------------

def test_registration_state_is_not_an_authentication_state(gated, monkeypatch):
    monkeypatch.setattr(pk, "get_credential", lambda cid: _stored_cred())
    tok = st.mint("webauthn-reg", {"challenge": "YWJj", "rp": RP}, ttl_seconds=60)
    c = TestClient(app, base_url=HTTPS)
    r = c.post("/api/passkey/auth", json={"state": tok, "credential": {"rawId": "credid1"}})
    assert r.status_code == 401
    assert "expired" in r.json()["detail"].lower() or "again" in r.json()["detail"].lower()


def test_session_token_is_not_an_authentication_state(gated):
    sess = users.create_session_token("susan")
    c = TestClient(app, base_url=HTTPS)
    r = c.post("/api/passkey/auth", json={"state": sess, "credential": {"rawId": "credid1"}})
    assert r.status_code == 401


# ---- 4. challenge replay ----------------------------------------------------

def test_a_challenge_cannot_be_used_twice(gated, monkeypatch):
    monkeypatch.setattr(pk, "get_credential", lambda cid: _stored_cred())

    class FakeVerified:
        new_sign_count = 1
    monkeypatch.setattr(m._webauthn, "verify_authentication_response",
                        lambda **kw: FakeVerified())
    monkeypatch.setattr(pk, "touch", lambda rid, sc: None)

    tok = st.mint("webauthn-auth", {"challenge": "Y2hhbGxlbmdl", "rp": RP}, ttl_seconds=60)
    body = {"state": tok, "credential": {"rawId": "credid1", "response": {}}}
    c = TestClient(app, base_url=HTTPS)
    first = c.post("/api/passkey/auth", json=body)
    assert first.status_code == 200 and first.json()["ok"] is True
    second = c.post("/api/passkey/auth", json=body)
    assert second.status_code == 401
    assert "already used" in second.json()["detail"]


# ---- 5. store outage honesty ------------------------------------------------

def test_store_outage_is_503_and_never_a_failed_attempt(gated, monkeypatch):
    def boom(cid):
        raise pk.PasskeyStoreUnavailable("airtable down")
    monkeypatch.setattr(pk, "get_credential", boom)
    tok = st.mint("webauthn-auth", {"challenge": "Y2hhbGxlbmdl", "rp": RP}, ttl_seconds=60)
    c = TestClient(app, base_url=HTTPS)
    r = c.post("/api/passkey/auth", json={"state": tok, "credential": {"rawId": "credid1"}})
    assert r.status_code == 503
    assert "access code" in r.json()["detail"]
    assert m._unlock_attempts == {}, "an outage was charged as a failed attempt"


def test_auth_options_outage_is_503_pointing_at_the_code(gated, monkeypatch):
    def boom(rp_id=None):
        raise pk.PasskeyStoreUnavailable("airtable down")
    monkeypatch.setattr(pk, "list_credentials", boom)
    c = TestClient(app, base_url=HTTPS)
    r = c.post("/api/passkey/auth-options")
    assert r.status_code == 503
    assert "access code" in r.json()["detail"]


# ---- 6. success mints the SAME gate cookie ----------------------------------

def test_verified_assertion_sets_the_unlock_cookie(gated, monkeypatch):
    monkeypatch.setattr(pk, "get_credential", lambda cid: _stored_cred())

    class FakeVerified:
        new_sign_count = 7
    monkeypatch.setattr(m._webauthn, "verify_authentication_response",
                        lambda **kw: FakeVerified())
    touched = []
    monkeypatch.setattr(pk, "touch", lambda rid, sc: touched.append((rid, sc)))

    tok = st.mint("webauthn-auth", {"challenge": "Y2hhbGxlbmdl", "rp": RP}, ttl_seconds=60)
    c = TestClient(app, base_url=HTTPS)
    r = c.post("/api/passkey/auth", json={"state": tok,
                                          "credential": {"rawId": "credid1", "response": {}}})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert c.cookies.get("cc_access") == CODE, "did not mint the same cookie /api/unlock mints"
    assert touched == [("recX", 7)]
    # And the gate now lets this client through, exactly like a typed code.
    monkeypatch.setattr(m.users, "list_users", lambda: [])
    assert c.get("/api/support/report").status_code == 200


def test_failed_verification_is_counted_and_visible(gated, monkeypatch):
    monkeypatch.setattr(pk, "get_credential", lambda cid: _stored_cred())

    def rejects(**kw):
        raise ValueError("bad signature")
    monkeypatch.setattr(m._webauthn, "verify_authentication_response", rejects)
    tok = st.mint("webauthn-auth", {"challenge": "Y2hhbGxlbmdl", "rp": RP}, ttl_seconds=60)
    c = TestClient(app, base_url=HTTPS)
    r = c.post("/api/passkey/auth", json={"state": tok,
                                          "credential": {"rawId": "credid1", "response": {}}})
    assert r.status_code == 401
    assert "access code" in r.json()["detail"]
    assert len(m._unlock_attempts) == 1  # charged like a wrong code
    kinds = [e["kind"] for e in support.report()["events"]]
    assert "passkey_failed" in kinds  # visible on /support
