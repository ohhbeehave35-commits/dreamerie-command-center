"""Remote support: the app must be observable without a human middleman.

Built after 28 Jul, when the only telemetry for a broken afternoon was Susan
describing her screen. These tests pin the four properties that make the
support view trustworthy:
  1. the recorder keeps faults and ONLY faults (no activity log),
  2. secrets can never sit in the buffer,
  3. the browser beacon is reachable WITHOUT the gate (a locked-out user is
     exactly who needs to be seen) but rate-limited and length-capped,
  4. the report endpoint and page are behind the gate.
"""

import os

import pytest
from fastapi.testclient import TestClient

import app.main as m
import app.support as support
from app.main import app


CODE = "correct-horse-battery-staple-42"
HTTPS = "https://testserver"  # gate cookie is secure=True; see test_access_gate_unlock


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


def _authed_client(gated_unused=None):
    c = TestClient(app, base_url=HTTPS)
    r = c.post("/api/unlock", json={"code": CODE})
    assert r.status_code == 200
    return c


# ---- 1. faults only -------------------------------------------------------

def test_successful_fast_requests_are_not_recorded():
    support.record_request("GET", "/api/history", 200, 120)
    support.record_request("GET", "/healthz", 200, 5)
    assert support.report()["events"] == []


def test_failures_and_slow_requests_are_recorded():
    support.record_request("POST", "/api/chat", 500, 1200, error="RuntimeError: boom")
    support.record_request("GET", "/api/settings", 200, support.SLOW_MS + 1)
    kinds = {e["kind"] for e in support.report()["events"]}
    assert kinds == {"server_error", "slow"}


def test_query_strings_are_never_stored():
    support.record_request("GET", "/api/leads?email=susan@example.com&token=abc12345", 500, 10)
    ev = support.report()["events"][0]
    assert ev["path"] == "/api/leads"
    assert "susan@example.com" not in str(ev)


# ---- 2. secrets never sit in the buffer -----------------------------------

def test_env_secret_values_are_scrubbed(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-verysecretvalue123456")
    support.record_request("POST", "/api/chat", 500, 10,
                           error="auth failed for key sk-ant-verysecretvalue123456 (401)")
    ev = support.report()["events"][0]
    assert "sk-ant-verysecretvalue123456" not in str(ev)
    assert "***" in ev["detail"]


def test_bearer_and_keyvalue_shapes_are_scrubbed():
    support.record_note("probe", "upstream said: Bearer abcdef123456789 / api_key=topsecret99")
    detail = support.report()["events"][0]["detail"]
    assert "abcdef123456789" not in detail
    assert "topsecret99" not in detail


def test_integration_flags_expose_names_never_values(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_supersecret000000")
    flags = {f["name"]: f["on"] for f in support.report()["integrations"]}
    assert flags["Stripe"] is True
    assert "sk_live_supersecret000000" not in str(support.report())


# ---- 3. the beacon: ungated, capped, but useful ----------------------------

def test_client_event_is_reachable_without_the_gate(gated):
    c = TestClient(app, base_url=HTTPS)  # NOT unlocked
    r = c.post("/api/support/client-event", json={
        "kind": "client_network", "path": "/api/chat", "method": "POST",
        "detail": "Failed to fetch after 30012ms",
    })
    assert r.status_code == 202
    ev = support.report()["events"][0]
    assert ev["kind"] == "client_network"
    assert ev["source"] == "browser (not signed in)"


def test_client_event_from_unlocked_browser_is_marked_authenticated(gated):
    c = _authed_client()
    c.post("/api/support/client-event", json={"kind": "client_error", "path": "/x", "detail": "boom"})
    assert support.report()["events"][0]["source"] == "browser"


def test_client_event_rate_limit_holds(gated):
    c = TestClient(app, base_url=HTTPS)
    for _ in range(support.MAX_CLIENT_EVENTS_PER_IP + 10):
        c.post("/api/support/client-event", json={"kind": "client_error", "detail": "x"})
    assert len(support.report()["events"]) == support.MAX_CLIENT_EVENTS_PER_IP


def test_client_event_ignores_unknown_kinds_and_caps_length(gated):
    c = TestClient(app, base_url=HTTPS)
    c.post("/api/support/client-event", json={
        "kind": "definitely_not_a_kind", "detail": "A" * 5000, "path": "/p",
    })
    ev = support.report()["events"][0]
    assert ev["kind"] == "client_error"           # coerced, not trusted
    assert len(ev["detail"]) <= support.MAX_FIELD


# ---- 4. report + page are behind the gate ----------------------------------

def test_report_endpoint_is_gated(gated):
    c = TestClient(app, base_url=HTTPS)
    assert c.get("/api/support/report").status_code == 401
    assert c.get("/support").status_code == 401


def test_report_endpoint_works_when_unlocked(gated):
    c = _authed_client()
    r = c.get("/api/support/report")
    assert r.status_code == 200
    body = r.json()
    for key in ("rev", "booted_at", "uptime_seconds", "events", "integrations"):
        assert key in body
    assert c.get("/support").status_code == 200


# ---- unlock failures become visible ----------------------------------------

def test_failed_unlock_is_recorded_without_the_code(gated):
    c = TestClient(app, base_url=HTTPS)
    c.post("/api/unlock", json={"code": "wrong-guess-hunter2"})
    evs = [e for e in support.report()["events"] if e["kind"] == "unlock_failed"]
    assert len(evs) == 1
    assert "wrong-guess-hunter2" not in str(evs[0])


# ---- restart visibility -----------------------------------------------------

def test_report_names_the_serving_revision(monkeypatch, gated):
    monkeypatch.setenv("RENDER_GIT_COMMIT", "abc123def456789")
    c = _authed_client()
    assert c.get("/api/support/report").json()["rev"] == "abc123def456"
