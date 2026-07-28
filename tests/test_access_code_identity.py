"""An access-code login is a real identity, not an expired session.

Susan's build has no per-user login -- the only way in is the shared access
code. But /api/history and /api/me demanded a per-user session cookie and 401'd
without one, so the frontend showed a permanent "Your session expired -- sign in
again" banner to a correctly-authenticated user, with no login form to sign in
to. Chat worked (it used the lenient scoper), history did not (strict scoper),
and they didn't even agree on which bucket to use.

These assert that a valid access cookie is treated as an identity end to end,
while a request with NO credentials is still refused, and a real per-user
session still gets its own isolated bucket.
"""

import pytest
from fastapi.testclient import TestClient

import app.main as m
import app.users as users
from app.main import app


HTTPS = "https://testserver"
CODE = "Wildlife-test-code-99"


@pytest.fixture
def access_deployment(monkeypatch):
    """Gate ON, authenticated by access code, no per-user accounts -- exactly
    Susan's deployment."""
    monkeypatch.setattr(m, "ACCESS_CODE", "")
    monkeypatch.setattr(m.crm, "is_configured", lambda: True)
    monkeypatch.setattr(m.crm, "get_setting",
                        lambda k, d="": CODE if k == "access_code_override" else d)
    # history reads come from crm.get_history; stub it to echo the bucket so we
    # can prove which one was used.
    monkeypatch.setattr(m.crm, "get_history",
                        lambda limit, scoped: [{"role": "system", "content": scoped}])
    yield


def _with_code(c):
    c.cookies.set("cc_access", CODE)
    return c


def test_history_serves_for_an_access_code_user_instead_of_401(access_deployment):
    c = _with_code(TestClient(app, base_url=HTTPS))
    r = c.get("/api/history?chat_id=default")
    assert r.status_code == 200, "access-code user was refused -- this is the banner bug"
    body = r.json()
    assert body["authed"] is True
    # And it used a STABLE access bucket, not the raw unscoped one.
    assert body["history"][0]["content"] == "access:default"


def test_me_reports_signed_in_for_an_access_code_user(access_deployment):
    c = _with_code(TestClient(app, base_url=HTTPS))
    r = c.get("/api/me")
    assert r.status_code == 200
    assert r.json().get("access_mode") is True


def test_no_credentials_is_still_refused(access_deployment):
    """The anti-guessing protection must survive: a request with no valid
    access cookie and no session still gets 401, not the shared bucket."""
    c = TestClient(app, base_url=HTTPS)  # no cookie at all
    c.cookies.set("cc_access", "wrong-code")
    assert c.get("/api/history?chat_id=default").status_code == 401
    assert c.get("/api/me").status_code == 401


def test_per_user_session_still_gets_its_own_isolated_bucket(access_deployment, monkeypatch):
    """A real login must NOT be collapsed into the shared access bucket --
    multi-account isolation has to survive this change."""
    monkeypatch.setattr(users, "verify_session_token",
                        lambda t: "alice" if t == "alice-token" else None)
    monkeypatch.setattr(users, "user_exists_cached", lambda u: True)
    c = TestClient(app, base_url=HTTPS)
    c.cookies.set("cc_session", "alice-token")
    r = c.get("/api/history?chat_id=default")
    assert r.status_code == 200
    assert r.json()["history"][0]["content"] == "user:alice:default"


def test_chat_and_history_agree_on_the_bucket(access_deployment):
    """The original split: chat wrote to one bucket, history read another. Both
    scopers must now resolve an access-code user to the same place."""
    from starlette.requests import Request

    scope = {"type": "http", "headers": [(b"cookie", f"cc_access={CODE}".encode())]}
    req = Request(scope)
    assert m._scoped_chat_id(req, "default") == "access:default"
    assert m._scoped_chat_id_checked(req, "default") == "access:default"
