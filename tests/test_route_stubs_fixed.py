"""The 28 Jul route-stub audit, pinned.

Two flavors of fix: routes RESTORED because the UI still called them (/api/tts
-- Annabelle's voice; /artifact/{slug} -- every document link chat hands out),
and routes REMOVED because they could only 500 or fabricate (/terms with no
file, /capabilities with no file, the Buildertrend webhook with no module, the
dev-ticket branch that logged invented work as done).
"""

import pytest
from fastapi.testclient import TestClient

import app.main as m
from app.main import app


HTTPS = "https://testserver"


@pytest.fixture
def open_gate(monkeypatch):
    """No access code, no CRM -> the gate stays open (local-dev posture)."""
    monkeypatch.setattr(m, "ACCESS_CODE", "")
    monkeypatch.setattr(m.crm, "is_configured", lambda: False)


# ---- restored: /api/tts -----------------------------------------------------

def test_tts_route_exists(open_gate):
    """Empty text reaches the handler and gets its 400 -- not the router's 404.
    This is the assertion that fails on the shipped bug (route absent)."""
    c = TestClient(app, base_url=HTTPS)
    r = c.post("/api/tts", json={"text": "   "})
    assert r.status_code == 400, f"expected handler 400, got {r.status_code}"


def test_tts_returns_audio(open_gate, monkeypatch):
    async def fake_edge(text, voice=""):
        return b"ID3fakeaudio"
    monkeypatch.setattr(m, "_edge_tts", fake_edge)
    monkeypatch.setattr(m.voice_eleven, "is_configured", lambda: False)
    monkeypatch.setattr(m, "XAI_API_KEY", "")
    c = TestClient(app, base_url=HTTPS)
    r = c.post("/api/tts", json={"text": "Hello Susan"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("audio/mpeg")
    assert r.content == b"ID3fakeaudio"


# ---- restored: /artifact/{slug} --------------------------------------------

def test_artifact_route_renders_stored_document(open_gate, monkeypatch):
    monkeypatch.setattr(m.crm, "get_artifact",
                        lambda slug: {"title": "Pool Proposal", "content": "# Hi\nBody text"}
                        if slug == "abc123" else {})
    c = TestClient(app, base_url=HTTPS)
    r = c.get("/artifact/abc123")
    assert r.status_code == 200
    assert "Pool Proposal" in r.text
    assert "Body text" in r.text


def test_artifact_unknown_slug_is_a_clean_404(open_gate, monkeypatch):
    monkeypatch.setattr(m.crm, "get_artifact", lambda slug: {})
    c = TestClient(app, base_url=HTTPS)
    r = c.get("/artifact/nope")
    assert r.status_code == 404
    assert "invalid or has expired" in r.text


def test_artifact_is_reachable_without_auth(monkeypatch):
    """Client-facing links must work for CLIENTS -- people with no session and
    no access code. The gate exempts /artifact/ by prefix; prove it."""
    CODE = "correct-horse-battery-staple-42"
    monkeypatch.setattr(m, "ACCESS_CODE", "")
    monkeypatch.setattr(m.crm, "is_configured", lambda: True)
    monkeypatch.setattr(m.crm, "get_setting",
                        lambda k, d="": CODE if k == "access_code_override" else d)
    monkeypatch.setattr(m.crm, "get_artifact",
                        lambda slug: {"title": "Shared Doc", "content": "hello"})
    c = TestClient(app, base_url=HTTPS)  # NOT unlocked
    r = c.get("/artifact/shared1")
    assert r.status_code == 200
    assert "Shared Doc" in r.text


# ---- removed: the routes that could only 500 or lie -------------------------

def test_buildertrend_webhook_is_gone(open_gate):
    c = TestClient(app, base_url=HTTPS)
    assert c.post("/api/webhooks/buildertrend", json={}).status_code == 404


def test_capabilities_and_terms_are_gone_not_500(open_gate):
    c = TestClient(app, base_url=HTTPS)
    assert c.get("/capabilities").status_code == 404
    assert c.get("/terms").status_code == 404


def test_dev_ticket_never_claims_autonomous_execution(open_gate, monkeypatch):
    """Whatever the approval level says, a ticket is LOGGED, never 'queued for
    execution' -- the old branch wrote model-invented 'changed_files' into the
    log as if the work happened."""
    CODE = "correct-horse-battery-staple-42"
    monkeypatch.setattr(m, "ACCESS_CODE", "")
    monkeypatch.setattr(m.crm, "is_configured", lambda: True)
    monkeypatch.setattr(m.crm, "get_setting",
                        lambda k, d="": {"dev_approval_level": "full_auto",
                                         "access_code_override": CODE}.get(k, d))
    logged = []
    monkeypatch.setattr(m.crm, "save_dev_agent_log", lambda **kw: logged.append(kw))
    m._unlock_attempts.clear()
    c = TestClient(app, base_url=HTTPS)
    assert c.post("/api/unlock", json={"code": CODE}).status_code == 200
    r = c.post("/api/dev/ticket", json={"request": "add a feature"})
    assert r.status_code == 200
    assert r.json()["status"] == "logged"
    assert "queued" not in r.json()["detail"].lower()
    assert len(logged) == 1 and logged[0]["result"] == "Logged"
