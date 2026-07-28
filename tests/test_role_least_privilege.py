"""A blank Role cell must not mean owner.

users.py defaulted a missing Role to "owner" in both lookup_user and
list_users, so the entire owner-only boundary -- user management, password
resets -- was one empty Airtable cell wide. Rows created by the app always
write Role explicitly; a blank one only exists if hand-made, and a hand-made
row with no stated role must not be the most powerful kind.

Susan's login is deliberately untouched by this: the access-code path never
reads Role at all (verified: the gate checks the cc_access cookie against the
stored code and nothing else). Roles only gate session-cookie endpoints.
"""

import pytest
from fastapi.testclient import TestClient

import app.main as m
import app.users as users
from app.main import app


HTTPS = "https://testserver"


# --- the parser -------------------------------------------------------------

def test_blank_role_is_staff_not_owner():
    assert users._parse_role({}) == "staff"
    assert users._parse_role({"Role": ""}) == "staff"
    assert users._parse_role({"Role": "   "}) == "staff"
    assert users._parse_role({"Role": None}) == "staff"


def test_explicit_roles_pass_through():
    assert users._parse_role({"Role": "owner"}) == "owner"
    assert users._parse_role({"Role": "staff"}) == "staff"
    # Hand-typed padding is honoured, not silently demoted.
    assert users._parse_role({"Role": " owner "}) == "owner"


def test_lookup_user_applies_least_privilege(monkeypatch):
    """Through the real extraction path, not just the helper."""
    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"records": [{"id": "rec1", "fields": {
                "Username": "handmade", "PasswordHash": "x"}}]}  # no Role cell

    class _Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **k): return _Resp()

    monkeypatch.setattr(users.crm, "is_configured", lambda: True)
    monkeypatch.setattr(users, "_ensure_users_table", lambda: "tbl1")
    monkeypatch.setattr(users.httpx, "Client", _Client)
    u = users.lookup_user("handmade")
    assert u is not None
    assert u["role"] == "staff", "blank Role cell must not grant owner"


# --- the boundary it protects ----------------------------------------------

def test_blank_role_session_cannot_reset_passwords(monkeypatch):
    """End of the wire: a session for a hand-made row with no Role must be
    refused by the owner-only reset endpoint."""
    monkeypatch.setattr(m, "_get_session_username", lambda request: "handmade")
    monkeypatch.setattr(users, "get_user",
                        lambda u: {"username": u, "role": users._parse_role({}),
                                   "password_hash": "x"})
    called = []
    monkeypatch.setattr(users, "update_user_password",
                        lambda *a, **k: called.append(a) or True)
    r = TestClient(app, base_url=HTTPS).post("/api/users/susan/reset-password")
    assert r.status_code == 403
    assert called == []


def test_explicit_owner_still_passes_the_boundary(monkeypatch):
    """The fix must not lock out the real owner: an app-created row always has
    Role written, and that row must keep working exactly as before."""
    monkeypatch.setattr(m, "_get_session_role", lambda request: users._parse_role({"Role": "owner"}))
    monkeypatch.setattr(users, "get_user",
                        lambda u: {"username": u, "role": "staff", "password_hash": "x"})
    monkeypatch.setattr(users, "update_user_password", lambda u, pw: True)
    r = TestClient(app, base_url=HTTPS).post("/api/users/susan/reset-password")
    assert r.status_code == 200


def test_access_code_path_reads_no_role(monkeypatch):
    """The claim that makes this change safe to ship while Susan is using the
    app: her path must never call the user store at all."""
    CODE = "Wildlife-test-code-1"
    monkeypatch.setattr(m, "ACCESS_CODE", "")
    monkeypatch.setattr(m.crm, "is_configured", lambda: True)
    monkeypatch.setattr(m.crm, "get_setting",
                        lambda k, d="": CODE if k == "access_code_override" else d)

    def boom(*a, **k):
        raise AssertionError("access-code path touched the user store")
    monkeypatch.setattr(users, "get_user", boom)
    monkeypatch.setattr(users, "lookup_user", boom)
    monkeypatch.setattr(users, "list_users", boom)

    m._unlock_attempts.clear()
    c = TestClient(app, base_url=HTTPS)
    assert c.post("/api/unlock", json={"code": CODE}).status_code == 200
    assert c.get("/api/toolbox").status_code != 401
    m._unlock_attempts.clear()
