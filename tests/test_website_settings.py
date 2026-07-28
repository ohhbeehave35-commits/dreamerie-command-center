"""Per-business website settings, and the access-code owner gate they exposed.

Susan asked for a website slot in Settings. Adding the field surfaced a bigger
problem: /api/settings saves are owner-only via a session role, but her build
logs in with the shared access code and has no per-user session -- so is_owner
was always False and EVERY admin setting silently no-op'd. She could type, click
Save, see "Saved", and nothing persisted.

These assert the access-code user can now save, the website round-trips, and
Annabelle is actually told the URL (the field is useless if she can't use it).
"""

import pytest
from fastapi.testclient import TestClient

import app.main as m
import app.users as users
from app.main import app


HTTPS = "https://testserver"
CODE = "Wildlife-test-code-77"


@pytest.fixture
def access_deployment(monkeypatch):
    store = {}
    monkeypatch.setattr(m, "ACCESS_CODE", "")
    monkeypatch.setattr(m.crm, "is_configured", lambda: True)

    def get_setting(k, d=""):
        if k == "access_code_override":
            return CODE
        return store.get(k, d)

    monkeypatch.setattr(m.crm, "get_setting", get_setting)
    # sync kwarg + truthy return match the real signature: admin saves now run
    # synchronously and report failures, and a fake returning None would read
    # as "every write failed".
    def set_setting(k, v, sync=False):
        store[k] = v
        return True

    monkeypatch.setattr(m.crm, "set_setting", set_setting)
    # get_user must not accidentally return an owner; access-code users have none.
    monkeypatch.setattr(users, "get_user", lambda u: None)
    monkeypatch.setattr(m, "_get_session_username", lambda request: None)
    return store


def _c():
    c = TestClient(app, base_url=HTTPS)
    c.cookies.set("cc_access", CODE)
    return c


def test_access_code_user_can_save_a_website(access_deployment):
    store = access_deployment
    r = _c().post("/api/settings", json={"website_dreamerie": "thedreamerie.com"})
    assert r.status_code == 200
    # It persisted, and was normalised to a real URL.
    assert store.get("website__dreamerie") == "https://thedreamerie.com"


def test_access_code_user_could_not_save_before_the_gate_fix(access_deployment, monkeypatch):
    """Guard the regression directly: with the access-code branch removed,
    the same save must NOT persist -- proving the fix is what enables it."""
    store = access_deployment
    monkeypatch.setattr(m, "_access_authenticated", lambda request: False)
    _c().post("/api/settings", json={"website_dreamerie": "thedreamerie.com"})
    assert "website__dreamerie" not in store


def test_website_round_trips_through_get(access_deployment):
    c = _c()
    c.post("/api/settings", json={"website_suzy_d": "https://suzyd.example"})
    r = c.get("/api/settings")
    assert r.status_code == 200
    assert r.json()["website_suzy_d"] == "https://suzyd.example"


def test_blank_website_leaves_existing_untouched(access_deployment):
    store = access_deployment
    store["website__dreamerie"] = "https://kept.example"
    _c().post("/api/settings", json={"website_dreamerie": ""})
    assert store["website__dreamerie"] == "https://kept.example"


def test_annabelle_is_told_the_site_for_the_active_business(access_deployment):
    access_deployment["website__dreamerie"] = "https://thedreamerie.com"
    ctx = m._website_context("dreamerie")
    assert "https://thedreamerie.com" in ctx
    assert "The Dreamerie" in ctx
    # A different mode with no site set gets nothing.
    assert m._website_context("bear_arms") == ""


def test_combined_mode_lists_every_configured_site(access_deployment):
    access_deployment["website__dreamerie"] = "https://thedreamerie.com"
    access_deployment["website__suzy_d"] = "https://suzyd.example"
    ctx = m._website_context("combined")
    assert "thedreamerie.com" in ctx and "suzyd.example" in ctx


def test_normalize_url_adds_scheme_but_leaves_full_urls():
    assert m._normalize_url("thedreamerie.com") == "https://thedreamerie.com"
    assert m._normalize_url("https://x.com") == "https://x.com"
    assert m._normalize_url("http://x.com") == "http://x.com"
    assert m._normalize_url("  ") == ""
