"""Agent long-term memory (app/memory.py).

The load-bearing behaviors, mirroring the rest of the codebase:
- never raises; degrades to a string / [] when Airtable is absent
- the recall honesty contract: "couldn't reach the store" must NOT read like
  "nothing is saved" -- same discipline as the client-interview recall.
"""

import app.memory as mem


def _not_configured(monkeypatch):
    monkeypatch.setattr(mem.crm, "is_configured", lambda: False)


def test_save_degrades_gracefully_when_airtable_absent(monkeypatch):
    _not_configured(monkeypatch)
    ok, msg = mem.add_memory_checked("Susan prefers ACH, never card", tags="dreamerie")
    assert ok is False and "not connected" in msg.lower()


def test_save_requires_a_summary(monkeypatch):
    monkeypatch.setattr(mem.crm, "is_configured", lambda: True)
    ok, msg = mem.add_memory_checked("   ")
    assert ok is False and "summary" in msg.lower()


def test_string_wrapper_always_returns_a_string(monkeypatch):
    _not_configured(monkeypatch)
    out = mem.add_memory("anything")
    assert isinstance(out, str) and out


def test_list_returns_empty_list_not_error_when_absent(monkeypatch):
    _not_configured(monkeypatch)
    assert mem.list_memory_raw() == []


def test_recall_distinguishes_unreachable_from_empty(monkeypatch):
    """A store it couldn't reach is NOT the same as nothing being remembered --
    the exact confabulation trap this codebase keeps closing."""
    _not_configured(monkeypatch)
    out = mem.recall_memory("pricing")
    assert "different from nothing" in out.lower()
    # and it must not falsely assert there are no memories
    assert "nothing in memory matches" not in out.lower()
