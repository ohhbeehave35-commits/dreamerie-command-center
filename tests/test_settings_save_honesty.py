"""A Save button must never say "Saved." over a write that didn't land.

The original set_setting persisted in a daemon thread that (a) never checked
the write's HTTP status and (b) swallowed every exception -- so an Airtable 403
was a *silent success*: the UI said Saved, the cache showed the value for 30
seconds, then it reverted. That is the exact shape of "Susan's Zapier webhooks
keep not saving". These tests pin the honest behaviour.
"""

import pytest
from fastapi.testclient import TestClient

import app.crm as crm
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


def test_admin_save_reports_failed_writes(gated, monkeypatch):
    """Airtable refuses the write -> the response says WHICH fields failed,
    with a non-2xx status, instead of 'Saved.'"""
    monkeypatch.setattr(m.crm, "set_setting", lambda k, v, sync=False: False)
    c = _unlocked()
    r = c.post("/api/settings", json={
        "zapier_webhook_url_tiktok": "https://hooks.zapier.com" + "/hooks/catch/" + "000000/fakehook/",
    })
    assert r.status_code == 502
    body = r.json()
    assert body["ok"] is False
    assert "tiktok webhook" in body["failed"]


def test_admin_save_succeeds_when_writes_land(gated, monkeypatch):
    monkeypatch.setattr(m.crm, "set_setting", lambda k, v, sync=False: True)
    c = _unlocked()
    r = c.post("/api/settings", json={
        "zapier_webhook_url_tiktok": "https://hooks.zapier.com" + "/hooks/catch/" + "000000/fakehook/",
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_sync_set_setting_returns_false_and_reports_on_airtable_error(monkeypatch):
    """Drive the real set_setting with a failing Airtable write and confirm:
    False comes back, the optimistic cache entry is dropped, and the failure
    lands on the support page."""
    monkeypatch.setattr(crm, "is_configured", lambda: True)
    monkeypatch.setattr(crm, "_ensure_settings_table", lambda: "tblX")

    class _FailingResponse:
        status_code = 403
        text = "insufficient permissions"
        def raise_for_status(self):
            raise RuntimeError("HTTP 403: insufficient permissions")
        def json(self):
            return {"records": []}

    class _FailingClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **k):
            ok = _FailingResponse()
            ok.raise_for_status = lambda: None  # the read works; the WRITE fails
            return ok
        def post(self, *a, **k): return _FailingResponse()
        def patch(self, *a, **k): return _FailingResponse()

    monkeypatch.setattr(crm.httpx, "Client", _FailingClient)
    # Seed the cache so we can verify the optimistic entry is rolled back.
    crm._settings_cache_at = 1.0
    crm._settings_cache = {}

    ok = crm.set_setting("zapier_webhook_url_tiktok", "https://hooks.example", sync=True)
    assert ok is False
    assert "zapier_webhook_url_tiktok" not in crm._settings_cache
    kinds = [e["kind"] for e in support.report()["events"]]
    assert "setting_write_failed" in kinds

    crm._settings_cache_at = 0.0
    crm._settings_cache = {}


def test_sync_set_setting_returns_true_on_success(monkeypatch):
    monkeypatch.setattr(crm, "is_configured", lambda: True)
    monkeypatch.setattr(crm, "_ensure_settings_table", lambda: "tblX")

    class _OkResponse:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"records": []}

    class _OkClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **k): return _OkResponse()
        def post(self, *a, **k): return _OkResponse()
        def patch(self, *a, **k): return _OkResponse()

    monkeypatch.setattr(crm.httpx, "Client", _OkClient)
    assert crm.set_setting("some_key", "some_value", sync=True) is True
