"""The hands must be honest hands.

browser_drive is the first tool in this app that ACTS on the live web, so the
suite proves the guardrails, not just the happy path:
- nothing is offered/attempted without a key (honest 'not connected')
- the SSRF rule holds for the entry URL, every goto step, and the FINAL page
- step validation refuses junk before a browser-minute is spent
- the Browserbase session is released even when driving explodes
- the CDP code path really drives a page (against a LOCAL headless Chrome,
  so no Browserbase minutes are spent proving it)
"""

import pytest

from app import browser_drive, browserbase


# ---- 1. honest when unconfigured -------------------------------------------

def test_unconfigured_says_not_connected(monkeypatch):
    monkeypatch.delenv("BROWSERBASE_API_KEY", raising=False)
    ok, why = browser_drive.drive("https://example.com", [])
    assert not ok
    assert "isn't connected" in why
    assert "scrape_page" in why  # points at what still works


# ---- 2. SSRF rule holds everywhere ------------------------------------------

def test_private_entry_url_is_refused(monkeypatch):
    monkeypatch.setenv("BROWSERBASE_API_KEY", "test-key")
    for url in ("http://127.0.0.1/admin", "http://192.168.1.1/",
                "http://localhost:8000/", "ftp://example.com"):
        ok, why = browser_drive.drive(url, [])
        assert not ok, url
        assert "refused" in why or "http(s)" in why


def test_private_goto_step_is_refused_before_any_session(monkeypatch):
    monkeypatch.setenv("BROWSERBASE_API_KEY", "test-key")
    created = []
    monkeypatch.setattr(browserbase, "create_session",
                        lambda: created.append(1) or (False, "should not get here"))
    ok, why = browser_drive.drive(
        "https://example.com",
        [{"do": "goto", "url": "http://169.254.169.254/latest/meta-data"}])
    assert not ok
    assert "refused" in why
    assert created == [], "a session was created before validation finished"


class _FakePage:
    """Lands on a private address after a redirect."""
    url = "http://10.0.0.5/internal"

    def set_default_timeout(self, ms):
        pass

    def goto(self, url, **kw):
        pass

    def title(self):
        return "internal"

    def evaluate(self, js):
        return "SECRET INTERNAL CONTENT"


def test_final_page_on_private_host_is_not_read():
    out = browser_drive._run_steps(_FakePage(), "https://example.com", [])
    assert out["text"] == ""
    assert any("refused to read the final page" in n for n in out["notes"])


# ---- 3. step validation refuses junk cheaply --------------------------------

def test_step_validation_catches_shape_errors(monkeypatch):
    monkeypatch.setenv("BROWSERBASE_API_KEY", "test-key")
    monkeypatch.setattr(browserbase, "create_session",
                        lambda: (_ for _ in ()).throw(AssertionError("no session for bad steps")))
    cases = [
        ([{"do": "hack"}], "invalid"),
        ([{"do": "click"}], "target"),
        ([{"do": "type", "target": "#q"}], "text"),
        ([{"do": "wait", "seconds": 99}], "0-10"),
        ([{}] * 9, "cap"),
        ("not a list", "list"),
    ]
    for steps, needle in cases:
        ok, why = browser_drive.drive("https://example.com", steps)
        assert not ok, steps
        assert needle in why, (steps, why)


# ---- 4. the meter always stops ----------------------------------------------

def test_session_released_even_when_driving_explodes(monkeypatch):
    monkeypatch.setenv("BROWSERBASE_API_KEY", "test-key")
    released = []
    monkeypatch.setattr(browserbase, "create_session",
                        lambda: (True, {"id": "sess-1", "connect_url": "ws://nowhere",
                                        "replay_url": "https://replay/sess-1"}))
    monkeypatch.setattr(browserbase, "release_session",
                        lambda sid: released.append(sid) or (True, "released"))
    ok, why = browser_drive.drive("https://example.com", [])
    assert not ok                       # ws://nowhere can't connect
    assert released == ["sess-1"], "the session kept billing"
    assert "replay" in why              # failure still hands over the replay link



# The live-CDP round-trip test lives in the flagship only -- it needs the
# playwright package, which these deployments don't install until they
# actually have a Browserbase key. Everything above is the guardrail surface
# and runs everywhere.
