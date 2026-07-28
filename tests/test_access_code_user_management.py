"""The access-code holder IS the owner -- including for user management.

Susan's Settings showed an "Invite a team member" form whose every request
403'd, because /api/users (and delete / reset-password) gated on the SESSION
role, which an access-code deployment never has. She needs that form to create
Nick's login. Same fix, same reasoning as the /api/settings owner check.
"""

import pytest
from fastapi.testclient import TestClient

import app.main as m
import app.support as support
from app.main import app


CODE = "correct-horse-battery-staple-42"
HTTPS = "https://testserver"


@pytest.fixture(autouse=True)
def clean_buffer():
    support.reset_for_tests()
    yield
    support.reset_for_tests()


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setattr(m, "ACCESS_CODE", "")
    monkeypatch.setattr(m.crm, "is_configured", lambda: True)
    monkeypatch.setattr(m.crm, "get_setting",
                        lambda k, d="": CODE if k == "access_code_override" else d)
    m._unlock_attempts.clear()
    yield
    m._unlock_attempts.clear()


def _unlocked() -> TestClient:
    c = TestClient(app, base_url=HTTPS)
    assert c.post("/api/unlock", json={"code": CODE}).status_code == 200
    return c


def test_access_code_owner_can_list_users(gated, monkeypatch):
    monkeypatch.setattr(m.users, "list_users", lambda: [
        {"username": "nick", "role": "owner"},
    ])
    r = _unlocked().get("/api/users")
    assert r.status_code == 200
    assert r.json()["users"][0]["username"] == "nick"


def test_access_code_owner_can_add_a_user(gated, monkeypatch):
    created = {}

    def fake_add_user(username, email, password, role):
        created.update(username=username, email=email, role=role)
        return True

    monkeypatch.setattr(m.users, "validate_username", lambda u: (True, ""))
    monkeypatch.setattr(m.users, "validate_password", lambda p: (True, ""))
    monkeypatch.setattr(m.users, "add_user", fake_add_user)
    monkeypatch.setattr(m.crm, "set_user_setting", lambda *a, **k: None)

    r = _unlocked().post("/api/users", json={
        "username": "nick", "email": "nick@example.com",
        "password": "a-strong-password-1", "role": "owner",
    })
    assert r.status_code == 200 and r.json()["ok"] is True
    assert created == {"username": "nick", "email": "nick@example.com", "role": "owner"}


def test_user_management_still_locked_without_any_auth(gated):
    c = TestClient(app, base_url=HTTPS)  # no session, no access cookie
    # The gate middleware answers first -- 401, not the endpoint's 403.
    assert c.get("/api/users").status_code == 401
    assert c.post("/api/users", json={
        "username": "x", "email": "x@x.com", "password": "p", "role": "staff",
    }).status_code == 401


def test_session_staff_user_still_cannot_manage_users(gated, monkeypatch):
    """The widened check must not widen past the owner: a logged-in STAFF
    session with no access cookie stays 403."""
    monkeypatch.setattr(m.users, "verify_session_token",
                        lambda t: "staffer" if t == "staff-token" else None)
    monkeypatch.setattr(m.users, "user_exists_cached", lambda u: True)
    monkeypatch.setattr(m.users, "get_user",
                        lambda u: {"username": u, "role": "staff"} if u == "staffer" else None)
    c = TestClient(app, base_url=HTTPS)
    c.cookies.set("cc_session", "staff-token")
    assert c.get("/api/users").status_code == 403


def test_owner_chat_setup_carries_the_current_date(gated):
    """Annabelle gets a fresh clock every turn -- date-sensitive tasks anchor
    to the real 'now', not training data."""
    sys_prompt, _tools = m._owner_chat_setup(m.ChatRequest(message="hi", history=[], mode="combined"))
    assert "CURRENT DATE & TIME:" in sys_prompt
    import datetime
    year = str(datetime.datetime.now().year)
    assert year in sys_prompt.split("CURRENT DATE & TIME:")[1][:120]
