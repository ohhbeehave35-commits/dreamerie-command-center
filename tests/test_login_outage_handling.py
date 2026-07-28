"""A datastore outage must not be reported as a wrong password.

get_user() catches every exception and returns None, which the login path could
not tell apart from "no such user". So an Airtable blip was charged to the
per-IP lockout AND to the deployment-wide backstop, and the person was told
their credentials were bad. Five blips locked a real user out for fifteen
minutes; a longer spell locked out everyone, each of them told it was their
password.

Found by an adversarial review of the recovery design, then confirmed against
the real source before fixing.
"""

import pytest
from fastapi.testclient import TestClient

import app.main as m
import app.users as users
from app.main import app


HTTPS = "https://testserver"


@pytest.fixture(autouse=True)
def open_gate(monkeypatch):
    """/api/login is gate-exempt, but keep the gate off so nothing else
    interferes with what these tests are measuring."""
    monkeypatch.setattr(m, "ACCESS_CODE", "")
    monkeypatch.setattr(m.crm, "is_configured", lambda: False)
    m._unlock_attempts.clear()
    m._global_unlock_attempts.clear()
    yield
    m._unlock_attempts.clear()
    m._global_unlock_attempts.clear()


def _outage(monkeypatch):
    def boom(username):
        raise users.UserLookupUnavailable("simulated Airtable failure")
    monkeypatch.setattr(users, "lookup_user", boom)


def test_outage_returns_503_not_invalid_credentials(monkeypatch):
    _outage(monkeypatch)
    r = TestClient(app, base_url=HTTPS).post(
        "/api/login", json={"username": "susan", "password": "whatever"})
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert "temporarily unavailable" in detail
    assert "Invalid username or password" not in detail


def test_outage_is_not_charged_to_the_per_ip_lockout(monkeypatch):
    """The bug that turned a 60-second outage into a 15-minute lockout."""
    _outage(monkeypatch)
    c = TestClient(app, base_url=HTTPS)
    for _ in range(m.UNLOCK_MAX_ATTEMPTS + 3):
        assert c.post("/api/login", json={"username": "susan", "password": "x"}).status_code == 503
    assert all(not v for v in m._unlock_attempts.values()), "outage fed the per-IP lockout"


def test_outage_is_not_charged_to_the_global_backstop(monkeypatch):
    """Worse than the per-IP case: the global counter locks out EVERY user of
    the deployment, so an outage would have taken the owner down too."""
    _outage(monkeypatch)
    c = TestClient(app, base_url=HTTPS)
    for _ in range(m.UNLOCK_MAX_ATTEMPTS + 3):
        c.post("/api/login", json={"username": "susan", "password": "x"})
    assert m._global_unlock_attempts == [], "outage fed the deployment-wide backstop"


def test_a_genuinely_missing_user_still_counts_as_a_failed_attempt(monkeypatch):
    """The fix must not blunt real brute-force protection."""
    monkeypatch.setattr(users, "lookup_user", lambda u: None)
    c = TestClient(app, base_url=HTTPS)
    r = c.post("/api/login", json={"username": "nobody", "password": "x"})
    assert r.status_code == 401
    assert "Invalid username or password" in r.json()["detail"]
    assert sum(len(v) for v in m._unlock_attempts.values()) == 1
    assert len(m._global_unlock_attempts) == 1


def test_get_user_contract_is_unchanged_for_its_other_callers(monkeypatch):
    """12 call sites rely on get_user returning None on failure. Changing that
    quietly would be a far bigger blast radius than the bug being fixed."""
    def boom(username):
        raise RuntimeError("airtable down")
    monkeypatch.setattr(users, "_ensure_users_table", boom)
    monkeypatch.setattr(users.crm, "is_configured", lambda: True)
    assert users.get_user("susan") is None
    with pytest.raises(users.UserLookupUnavailable):
        users.lookup_user("susan")


def test_lookup_user_returns_none_for_genuine_not_found(monkeypatch):
    """Not-found must NOT raise, or every unknown username becomes a 503 and
    the endpoint starts leaking which accounts exist."""
    monkeypatch.setattr(users.crm, "is_configured", lambda: False)
    assert users.lookup_user("nobody") is None
