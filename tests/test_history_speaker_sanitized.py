"""History must be stripped to role+content before it reaches the API.

Conversation history round-trips through the browser: get_history() decorates
each turn with a "speaker" field for the UI badge, the frontend sends the same
objects back, and main passed them to Anthropic verbatim. Anthropic 400s on
any unexpected key -- "messages.0.speaker: Extra inputs are not permitted" --
so ONE tagged turn in a reloaded conversation turned every later message in it
into a 500. Caught live on the /support page the night the remote was
installed (29 Jul 2026); Annabelle answered "feature update?" with a 500.
"""

import pytest

import app.main as m


class _Stop(Exception):
    """Raised by the fake stream so the test ends at the API boundary."""


def _capture_messages(monkeypatch):
    seen = {}

    class FakeStream:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def __enter__(self):
            raise _Stop()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(m.client.messages, "stream", lambda **kw: FakeStream(**kw))
    return seen


def _drive(history):
    events = m._run_main_brain_events(
        "what's new?", history, "system prompt", tools=[], enable_search=False,
    )
    with pytest.raises(_Stop):
        for _ in events:
            pass


def test_speaker_key_never_reaches_the_api(monkeypatch):
    seen = _capture_messages(monkeypatch)
    _drive([
        {"role": "user", "content": "hi", "speaker": "Susan"},
        {"role": "assistant", "content": "hey", "speaker": ""},
    ])
    msgs = seen["messages"]
    assert len(msgs) == 3  # 2 history + the new user turn
    for msg in msgs:
        assert set(msg.keys()) <= {"role", "content"}, f"extra keys leaked: {msg}"


def test_unknown_ui_fields_are_also_stripped(monkeypatch):
    """Whitelist, not a speaker-specific blacklist -- the NEXT decoration the
    UI grows must not re-create the bug."""
    seen = _capture_messages(monkeypatch)
    _drive([{"role": "user", "content": "hi", "avatar": "x.png", "ts": 123}])
    for msg in seen["messages"]:
        assert set(msg.keys()) <= {"role", "content"}, f"extra keys leaked: {msg}"


def test_contentless_and_weird_role_turns_are_normalized(monkeypatch):
    seen = _capture_messages(monkeypatch)
    _drive([
        {"role": "system", "content": "injected"},   # not a valid history role
        {"role": "user", "content": ""},             # empty turn -- dropped
    ])
    msgs = seen["messages"]
    assert all(msg["role"] in ("user", "assistant") for msg in msgs)
    assert all(msg["content"] for msg in msgs[:-1])
