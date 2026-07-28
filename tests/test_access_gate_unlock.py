"""The access gate's door.

The lock screen's only control POSTs to /api/unlock. That route was deleted in
the multi-company upgrade while the lock screen calling it was kept, and it was
never in the gate's exempt list either -- so the middleware answered 401 before
routing and the form printed "Incorrect code" for every code, including the
right one. Combined with the gate keying its cookie check on the ACCESS_CODE
env var (while the code itself came from Settings), a user whose session
expired had no way back in at all.

These tests assert on the things that were actually broken, so they would have
failed against the shipped code rather than merely describing it.
"""

import secrets

import pytest
from fastapi.testclient import TestClient

import app.main as m
from app.main import app


CODE = "correct-horse-battery-staple-42"

# The gate cookie is set secure=True, matching the session cookie and correct
# for Render. A secure cookie is stored but NOT SENT over plain http, so a
# TestClient on http:// silently fails the gate for the wrong reason and the
# end-to-end test would look like a code bug. Drive it over https.
HTTPS = "https://testserver"


@pytest.fixture
def gated(monkeypatch):
    """Gate ON, code supplied the way the Settings panel supplies it --
    via the override, with the ACCESS_CODE env var left UNSET. That is exactly
    the configuration the old gate could not handle."""
    monkeypatch.setattr(m, "ACCESS_CODE", "")
    monkeypatch.setattr(m.crm, "is_configured", lambda: True)
    monkeypatch.setattr(m.crm, "get_setting",
                        lambda k, d="": CODE if k == "access_code_override" else d)
    m._unlock_attempts.clear()
    yield
    m._unlock_attempts.clear()


def test_unlock_route_exists_and_is_not_swallowed_by_the_gate(gated):
    """The regression that locked everyone out: the gate answered /api/unlock
    itself, so the route was unreachable even when it existed."""
    c = TestClient(app, base_url=HTTPS)
    r = c.post("/api/unlock", json={"code": "definitely-wrong"})
    assert r.status_code != 404, "route missing"
    assert r.json().get("detail") != "locked", "gate swallowed the unlock request"
    assert r.status_code == 401  # wrong code, reached the handler


def test_correct_code_unlocks_and_sets_the_gate_cookie(gated):
    c = TestClient(app, base_url=HTTPS)
    r = c.post("/api/unlock", json={"code": CODE})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert "cc_access" in r.cookies or "cc_access" in c.cookies


def test_cookie_from_unlock_actually_gets_you_through_the_gate(gated):
    """The end-to-end assertion that matters. Setting a cookie the gate then
    rejects is the second bug, and it is invisible if you only test the
    endpoint's response."""
    c = TestClient(app, base_url=HTTPS)
    assert c.post("/api/unlock", json={"code": CODE}).status_code == 200
    r = c.get("/api/toolbox")  # any gated endpoint
    assert r.status_code != 401, "gate still refused a freshly unlocked session"


def test_gate_honours_a_settings_code_with_no_env_var(gated):
    """Bug 2 directly: keyed on the env var, this branch never ran."""
    c = TestClient(app, base_url=HTTPS)
    c.cookies.set("cc_access", CODE)
    r = c.get("/api/toolbox")
    assert r.status_code != 401


def test_wrong_code_is_refused(gated):
    c = TestClient(app, base_url=HTTPS)
    assert c.post("/api/unlock", json={"code": CODE + "x"}).status_code == 401
    c2 = TestClient(app, base_url=HTTPS)
    c2.cookies.set("cc_access", "not-the-code")
    assert c2.get("/api/toolbox").status_code == 401


def test_empty_cookie_cannot_match_an_empty_code(monkeypatch):
    """compare_digest('', '') is True. Without the truthiness guard, a
    deployment with no code configured would let ANY request through."""
    monkeypatch.setattr(m, "ACCESS_CODE", "")
    monkeypatch.setattr(m.crm, "is_configured", lambda: True)
    monkeypatch.setattr(m.crm, "get_setting", lambda k, d="": d)  # no code anywhere
    c = TestClient(app, base_url=HTTPS)
    c.cookies.set("cc_access", "")
    assert c.get("/api/toolbox").status_code == 401


def test_no_code_configured_says_so_rather_than_blaming_the_typist(monkeypatch):
    monkeypatch.setattr(m, "ACCESS_CODE", "")
    monkeypatch.setattr(m.crm, "is_configured", lambda: True)
    monkeypatch.setattr(m.crm, "get_setting", lambda k, d="": d)
    m._unlock_attempts.clear()
    r = TestClient(app, base_url=HTTPS).post("/api/unlock", json={"code": "anything"})
    assert r.status_code == 503
    assert "No access code is configured" in r.json()["detail"]


def test_repeated_wrong_codes_rate_limit_that_ip(gated):
    c = TestClient(app, base_url=HTTPS)
    for _ in range(m.UNLOCK_MAX_ATTEMPTS):
        assert c.post("/api/unlock", json={"code": "nope"}).status_code == 401
    r = c.post("/api/unlock", json={"code": "nope"})
    assert r.status_code == 429
    assert "Try again" in r.json()["detail"]


def test_gate_lockout_does_not_use_the_global_backstop(gated):
    """/api/login has a global counter. Sharing it with the gate would let
    anyone lock every user out of the deployment from throwaway addresses."""
    m._global_unlock_attempts.clear()
    c = TestClient(app, base_url=HTTPS)
    for _ in range(m.UNLOCK_MAX_ATTEMPTS):
        c.post("/api/unlock", json={"code": "nope"})
    assert m._global_unlock_attempts == [], "gate failures fed the global backstop"


def test_healthz_stays_reachable_while_gated(gated):
    assert TestClient(app, base_url=HTTPS).get("/healthz").status_code == 200
