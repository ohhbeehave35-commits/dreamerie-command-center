"""The lock page has two doors now: the shared access code (Susan) and a
username sign-in (Nick). The access-code door must stay the default and must
keep working exactly as before -- adding a second door is worthless if it
breaks the first one.
"""

import pytest
from fastapi.testclient import TestClient

import app.main as m
from app.main import LOCK_PAGE, app


CODE = "correct-horse-battery-staple-42"
HTTPS = "https://testserver"


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setattr(m, "ACCESS_CODE", "")
    monkeypatch.setattr(m.crm, "is_configured", lambda: True)
    monkeypatch.setattr(m.crm, "get_setting",
                        lambda k, d="": CODE if k == "access_code_override" else d)
    m._unlock_attempts.clear()
    m._global_unlock_attempts.clear()
    yield
    m._unlock_attempts.clear()
    m._global_unlock_attempts.clear()


# ---- the markup itself ------------------------------------------------------

def test_lock_page_offers_both_doors():
    assert 'id="c"' in LOCK_PAGE            # access code input
    assert 'id="u"' in LOCK_PAGE and 'id="p"' in LOCK_PAGE  # username + password
    assert "/api/unlock" in LOCK_PAGE and "/api/login" in LOCK_PAGE
    assert 'id="toggleMode"' in LOCK_PAGE


def test_access_code_is_the_default_view():
    """codeBox visible, userBox hidden -- Susan must not have to think."""
    code_i = LOCK_PAGE.index('id="codeBox"')
    user_i = LOCK_PAGE.index('id="userBox"')
    assert "display:none" not in LOCK_PAGE[code_i:code_i + 120]
    assert "display:none" in LOCK_PAGE[user_i:user_i + 120]


def test_page_has_one_script_block_and_balanced_form():
    assert LOCK_PAGE.count("<script>") == 1 and LOCK_PAGE.count("</script>") == 1
    assert LOCK_PAGE.count("<form") == 1 and LOCK_PAGE.count("</form>") == 1


# ---- both doors still open --------------------------------------------------

def test_access_code_door_still_works(gated):
    c = TestClient(app, base_url=HTTPS)
    r = c.post("/api/unlock", json={"code": CODE})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert c.cookies.get("cc_access") == CODE


def test_username_door_reaches_the_handler_through_the_gate(gated, monkeypatch):
    """/api/login must be gate-exempt, or Nick's form 401s before routing --
    the exact failure that bricked /api/unlock in June."""
    monkeypatch.setattr(m.users, "lookup_user", lambda u: None)
    c = TestClient(app, base_url=HTTPS)
    r = c.post("/api/login", json={"username": "nick", "password": "whatever1"})
    assert r.json().get("detail") != "locked", "gate swallowed the login request"
    assert r.status_code == 401  # no such user, but it ROUTED


def test_username_door_signs_a_real_user_in(gated, monkeypatch):
    monkeypatch.setattr(m.users, "lookup_user",
                        lambda u: {"username": "nick", "password_hash": "h"} if u == "nick" else None)
    monkeypatch.setattr(m.users, "verify_password", lambda p, h: p == "nicks-password-1")
    monkeypatch.setattr(m.users, "update_last_login", lambda u: None)
    monkeypatch.setattr(m.users, "create_session_token", lambda u: "session-token-nick")
    c = TestClient(app, base_url=HTTPS)
    r = c.post("/api/login", json={"username": "nick", "password": "nicks-password-1",
                                   "remember_me": True})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert c.cookies.get("cc_session") == "session-token-nick"


def test_nicks_session_passes_the_gate_without_the_access_code(gated, monkeypatch):
    """The whole point: Nick gets in on his own account, never needing Susan's
    shared code."""
    monkeypatch.setattr(m.users, "verify_session_token",
                        lambda t: "nick" if t == "session-token-nick" else None)
    monkeypatch.setattr(m.users, "user_exists_cached", lambda u: True)
    # Full record shape -- /api/me reads email/created_at/last_login too.
    monkeypatch.setattr(m.users, "get_user", lambda u: {
        "username": u, "email": "nick@example.com", "role": "staff",
        "created_at": "2026-07-28", "last_login": "",
    })
    c = TestClient(app, base_url=HTTPS)
    c.cookies.set("cc_session", "session-token-nick")
    r = c.get("/api/me")
    assert r.status_code == 200
    assert r.json()["username"] == "nick" and r.json()["role"] == "staff"
