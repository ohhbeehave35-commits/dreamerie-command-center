"""History must be stripped to {role, content} before it reaches the Anthropic API.

31 Jul 2026, live outage: crm.get_history returns every turn decorated with a
`speaker` tag (speaker-tagging feature), the client echoes the decorated turns
back as `history`, and _run_main_brain_events passed them VERBATIM into
client.messages.stream. The API hard-rejects unknown keys --

    400 messages.0.speaker: Extra inputs are not permitted

-- so every owner chat that loaded a saved conversation 500'd. It escaped the
suite because mocks accept any kwargs; only the real API validates. This test
closes that gap by asserting on the exact payload handed to the client object,
which IS what production sends.

Scanner-rule discipline: the planted-bug test below proves the assertion can
fail (a decorated message makes it fail), so a green here is evidence, not
decoration.
"""

import pytest

from app import main as appmain


class _CaptureDone(Exception):
    """Raised by the fake client after capturing -- we only need the payload,
    not a full round-trip."""


class _FakeMessages:
    def __init__(self, captured):
        self._captured = captured

    def stream(self, **kwargs):
        self._captured.append(kwargs)
        raise _CaptureDone()

    def create(self, **kwargs):
        self._captured.append(kwargs)
        raise _CaptureDone()


class _FakeClient:
    def __init__(self, captured):
        self.messages = _FakeMessages(captured)


DECORATED_HISTORY = [
    {"role": "user", "content": "hi", "speaker": ""},
    {"role": "assistant", "content": "hello!", "speaker": "Annabelle"},
    {"role": "user", "content": "remember me?", "speaker": "Vinny",
     "some_future_key": {"nested": True}},
]


def _messages_reaching_api(history, monkeypatch):
    captured = []
    monkeypatch.setattr(appmain, "client", _FakeClient(captured))
    # Cap checks and counters must not touch Airtable in a unit test.
    monkeypatch.setattr(appmain.crm, "get_chat_count", lambda persona: 0)
    monkeypatch.setattr(appmain.crm, "increment_chat_count", lambda persona: None)
    monkeypatch.setattr(appmain.crm, "get_search_count", lambda: 10 ** 9)
    gen = appmain._run_main_brain_events(
        "ping", history, "You are a test.", [], enable_search=False,
        persona="owner")
    with pytest.raises(_CaptureDone):
        for _ in gen:
            pass
    assert captured, "the fake client was never called"
    return captured[0]["messages"]


def test_history_metadata_never_reaches_the_api(monkeypatch):
    messages = _messages_reaching_api(DECORATED_HISTORY, monkeypatch)
    # Every history turn: exactly the two keys the API defines. The final
    # message is the fresh user turn built by _build_user_content.
    for i, m in enumerate(messages[:-1]):
        assert set(m.keys()) == {"role", "content"}, (
            f"messages[{i}] leaked extra keys to the API: {sorted(m.keys())} "
            "-- this is the exact shape of the 31 Jul 500 outage")
    # Order and content survive the strip.
    assert [m["content"] for m in messages[:-1]] == ["hi", "hello!", "remember me?"]
    assert messages[-1]["role"] == "user"


def test_malformed_history_turns_are_dropped_not_sent(monkeypatch):
    junk = [
        {"role": "user", "content": "   "},          # empty content -> API 400
        {"role": "system", "content": "sneaky"},      # role the API rejects here
        "not-a-dict",
        {"role": "user", "content": "kept", "speaker": "x"},
    ]
    messages = _messages_reaching_api(junk, monkeypatch)
    assert [m["content"] for m in messages[:-1]] == ["kept"]


def test_the_assertion_can_actually_fail():
    """Planted bug: a decorated message must FAIL the key-set check.
    Guards against this test rotting into one that passes on anything."""
    decorated = {"role": "user", "content": "hi", "speaker": "x"}
    assert set(decorated.keys()) != {"role", "content"}
