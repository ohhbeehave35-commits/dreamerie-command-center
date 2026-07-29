"""Loading a client packet must be a verifiable write, not a chat request.

28 Jul 2026: three Bear Arms assets were "pushed" by typing instructions into
Annabelle's chat. The transcript showed the message, routing said "answered
directly" -- she never called save_asset -- and the library stayed empty. A
request the model may decline is not a write. POST /api/assets writes them and
reports per-asset what actually happened.
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


ONE = {"name": "Bear Arms logo", "url": "https://example.com/l.png",
       "media_type": "Photo", "tags": "bear-arms logo", "notes": "mark"}


def test_assets_are_actually_written(gated, monkeypatch):
    written = []

    def fake(name, url, media_type="Photo", tags="", notes=""):
        written.append({"name": name, "url": url, "type": media_type})
        return True, f'Saved "{name}"'

    monkeypatch.setattr(m.assets, "add_asset_checked", fake)
    r = _unlocked().post("/api/assets", json={"assets": [ONE]})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["saved"] == 1
    assert written == [{"name": "Bear Arms logo", "url": "https://example.com/l.png",
                        "type": "Photo"}]


def test_a_partial_write_is_not_reported_as_success(gated, monkeypatch):
    """The failure mode this endpoint exists to prevent: half a packet landing
    while the caller is told it worked."""
    def fake(name, url, media_type="Photo", tags="", notes=""):
        if "deck" in name:
            return False, "The asset store rejected it (HTTP 422): Unknown field name: \"Tags\""
        return True, f'Saved "{name}"'

    monkeypatch.setattr(m.assets, "add_asset_checked", fake)
    r = _unlocked().post("/api/assets", json={
        "assets": [ONE, {**ONE, "name": "Bear Arms pitch deck"}]})
    body = r.json()
    assert r.status_code == 207, "a partial write must not return a plain 200"
    assert body["ok"] is False and body["saved"] == 1 and body["total"] == 2
    failed = [x for x in body["results"] if not x["ok"]][0]
    assert "422" in failed["detail"] and "Tags" in failed["detail"]


def test_failures_reach_the_support_page(gated, monkeypatch):
    monkeypatch.setattr(m.assets, "add_asset_checked",
                        lambda *a, **k: (False, "The asset store rejected it (HTTP 500): boom"))
    _unlocked().post("/api/assets", json={"assets": [ONE]})
    blob = str(support.report())
    assert "asset_push_failed" in blob and "500" in blob


def test_push_requires_auth(gated):
    c = TestClient(app, base_url=HTTPS)
    assert c.post("/api/assets", json={"assets": [ONE]}).status_code == 401


def test_empty_payload_is_rejected(gated):
    r = _unlocked().post("/api/assets", json={"assets": []})
    assert r.status_code == 400


def test_add_asset_string_wrapper_still_works_for_the_eight_callers(monkeypatch):
    """add_asset() must keep returning a bare string -- the Dropbox/Drive
    importers and the generators pattern-match its text."""
    import app.assets as A
    monkeypatch.setattr(A, "add_asset_checked", lambda *a, **k: (True, 'Saved "x" (Photo)'))
    out = A.add_asset("x", "https://e.com/x.png")
    assert isinstance(out, str) and out.startswith("Saved")
