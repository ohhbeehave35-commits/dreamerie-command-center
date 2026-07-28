"""Owner-side password reset.

Before this endpoint the only way to recover a forgotten password was to delete
and recreate the account, which silently detaches chats and per-user settings
(both keyed by username). So the tests that matter are the ones proving the
endpoint is genuinely owner-gated and that it never hands back a password the
app would then reject.
"""

import pytest
from fastapi.testclient import TestClient

from app import password_reset, users
from app.main import app


client = TestClient(app)


# --- the generator ---------------------------------------------------------

def test_generated_password_always_passes_the_app_s_own_validator():
    """The endpoint promises a working password. If the generator can emit one
    that validate_password rejects, that promise breaks intermittently -- which
    is the worst way for it to break. Sample enough draws to catch a 1-in-N."""
    for _ in range(500):
        pw = password_reset.generate_temporary_password()
        ok, reason = users.validate_password(pw)
        assert ok, f"generated {pw!r} rejected: {reason}"


def test_generated_passwords_are_unique_and_long_enough():
    seen = {password_reset.generate_temporary_password() for _ in range(200)}
    assert len(seen) == 200, "temporary passwords must not repeat"
    assert all(len(p) >= 12 for p in seen)


def test_generator_excludes_ambiguous_glyphs():
    """These get read aloud and typed by hand; l/1/I and O/0 turn a 30-second
    unblock into another support round-trip."""
    joined = "".join(password_reset.generate_temporary_password() for _ in range(100))
    for ch in "lI1O0":
        assert ch not in joined, f"ambiguous character {ch!r} present"


def test_short_length_is_raised_not_honoured():
    assert len(password_reset.generate_temporary_password(4)) >= 12


# --- the endpoint ----------------------------------------------------------

def test_reset_requires_owner_session(monkeypatch):
    """No session at all must be refused -- and must NOT reveal whether the
    user exists."""
    called = []
    monkeypatch.setattr(users, "update_user_password",
                        lambda *a, **k: called.append(a) or True)
    r = client.post("/api/users/susan/reset-password")
    assert r.status_code == 403
    assert called == [], "password was changed despite an unauthenticated call"


def test_staff_session_cannot_reset_another_user(monkeypatch):
    """Privilege check is on ROLE, not merely on being logged in."""
    import app.main as m
    monkeypatch.setattr(m, "_get_session_role", lambda request: "staff")
    called = []
    monkeypatch.setattr(users, "update_user_password",
                        lambda *a, **k: called.append(a) or True)
    r = client.post("/api/users/susan/reset-password")
    assert r.status_code == 403
    assert called == []


def test_owner_reset_returns_a_usable_password_once(monkeypatch):
    import app.main as m
    monkeypatch.setattr(m, "_get_session_role", lambda request: "owner")
    monkeypatch.setattr(users, "get_user",
                        lambda u: {"username": u, "role": "staff", "password_hash": "x"})
    written = {}
    monkeypatch.setattr(users, "update_user_password",
                        lambda u, pw: written.update({"user": u, "pw": pw}) or True)

    r = client.post("/api/users/susan/reset-password")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert written["user"] == "susan"
    # The value handed back must be exactly the one stored -- not a lookalike.
    assert body["temporary_password"] == written["pw"]
    assert users.validate_password(body["temporary_password"])[0]


def test_unknown_user_is_404_and_changes_nothing(monkeypatch):
    import app.main as m
    monkeypatch.setattr(m, "_get_session_role", lambda request: "owner")
    monkeypatch.setattr(users, "get_user", lambda u: None)
    called = []
    monkeypatch.setattr(users, "update_user_password",
                        lambda *a, **k: called.append(a) or True)
    r = client.post("/api/users/nobody/reset-password")
    assert r.status_code == 404
    assert called == []


def test_storage_failure_does_not_claim_success(monkeypatch):
    """If Airtable write fails we must not hand over a password that was never
    saved -- the user would be told it works and it would not."""
    import app.main as m
    monkeypatch.setattr(m, "_get_session_role", lambda request: "owner")
    monkeypatch.setattr(users, "get_user",
                        lambda u: {"username": u, "role": "staff", "password_hash": "x"})
    monkeypatch.setattr(users, "update_user_password", lambda u, pw: False)
    r = client.post("/api/users/susan/reset-password")
    assert r.status_code == 500
    assert "temporary_password" not in r.json()


def test_response_warns_when_login_lockout_is_active(monkeypatch):
    """The lockout is per-IP, so a reset does NOT clear it. Without this warning
    the owner hands over a correct password that still fails, and concludes the
    reset is broken."""
    import time as _time

    import app.main as m
    monkeypatch.setattr(m, "_get_session_role", lambda request: "owner")
    monkeypatch.setattr(users, "get_user",
                        lambda u: {"username": u, "role": "staff", "password_hash": "x"})
    monkeypatch.setattr(users, "update_user_password", lambda u, pw: True)
    now = _time.time()
    monkeypatch.setitem(m._unlock_attempts, "203.0.113.9",
                        [now] * m.UNLOCK_MAX_ATTEMPTS)
    try:
        r = client.post("/api/users/susan/reset-password")
        assert r.status_code == 200
        assert "rate-limited" in r.json()["detail"]
    finally:
        m._unlock_attempts.pop("203.0.113.9", None)
