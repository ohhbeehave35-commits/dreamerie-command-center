"""The factory default credential (owner/changeme) is printed in this repo's
history -- it is a PUBLISHED password. These tests pin the two defenses:
startup no longer creates it, and login refuses it even when it verifies.
"""

import pytest
from fastapi.testclient import TestClient

import app.main as m
import app.support as support
from app.main import app


HTTPS = "https://testserver"


@pytest.fixture(autouse=True)
def clean_buffer():
    support.reset_for_tests()
    m._unlock_attempts.clear()
    m._global_unlock_attempts.clear()
    yield
    support.reset_for_tests()
    m._unlock_attempts.clear()
    m._global_unlock_attempts.clear()


def test_startup_never_creates_a_default_account(monkeypatch):
    monkeypatch.setattr(m.crm, "is_configured", lambda: True)
    monkeypatch.setattr(m.users, "list_users", lambda: [])
    created = []
    monkeypatch.setattr(m.users, "add_user", lambda *a, **k: created.append(a) or True)
    m.startup_init()
    assert created == [], "startup created an account -- the published-default behavior is back"


def test_startup_flags_a_surviving_owner_account(monkeypatch):
    monkeypatch.setattr(m.crm, "is_configured", lambda: True)
    monkeypatch.setattr(m.users, "list_users", lambda: [{"username": "owner", "role": "owner"}])
    m.startup_init()
    kinds = [e["kind"] for e in support.report()["events"]]
    assert "default_credential" in kinds


def test_login_refuses_the_published_default_even_when_it_verifies(monkeypatch):
    monkeypatch.setattr(m.users, "lookup_user",
                        lambda u: {"username": "owner", "password_hash": "h"} if u == "owner" else None)
    monkeypatch.setattr(m.users, "verify_password", lambda p, h: p == "changeme")
    c = TestClient(app, base_url=HTTPS)
    r = c.post("/api/login", json={"username": "owner", "password": "changeme"})
    assert r.status_code == 403
    assert "cc_session" not in r.cookies
    assert "default" in r.json()["detail"].lower()
    kinds = [e["kind"] for e in support.report()["events"]]
    assert "default_credential" in kinds


def test_login_with_a_real_password_still_works(monkeypatch):
    """The guard must catch ONLY the published pair -- a legitimately reset
    password on the same 'owner' username sails through."""
    monkeypatch.setattr(m.users, "lookup_user",
                        lambda u: {"username": "owner", "password_hash": "h"} if u == "owner" else None)
    monkeypatch.setattr(m.users, "verify_password", lambda p, h: p == "a-real-password-9")
    monkeypatch.setattr(m.users, "update_last_login", lambda u: None)
    monkeypatch.setattr(m.users, "create_session_token", lambda u: "tok")
    c = TestClient(app, base_url=HTTPS)
    r = c.post("/api/login", json={"username": "owner", "password": "a-real-password-9"})
    assert r.status_code == 200 and r.json()["ok"] is True
