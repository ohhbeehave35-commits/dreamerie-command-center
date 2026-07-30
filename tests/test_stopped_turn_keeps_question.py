"""Pressing stop must not discard the user's own message.

30 Jul 2026. Vinny pasted a YouTube URL, stopped the reply mid-stream, then
asked about the link. She answered "I don't see a link" -- true from the
server's side, indistinguishable from gaslighting from his.

Root cause: both chat endpoints skipped persistence entirely when the reply was
STOPPED_REPLY, and _persist_turn wrote the question and the answer in a single
call. So stopping a reply threw away the question with it. Not just links --
anything said in an interrupted turn was lost silently: a price, an address, a
decision.

These tests pin the split: the question is saved when it ARRIVES, the answer
when it COMPLETES, and a stop only costs the answer.
"""

import app.main as m


class _Spy:
    """Records save_turn calls instead of writing to Airtable."""

    def __init__(self):
        self.calls = []

    def __call__(self, role, content, chat_id="default", speaker=""):
        self.calls.append({"role": role, "content": content,
                           "chat_id": chat_id, "speaker": speaker})

    def roles(self):
        return [c["role"] for c in self.calls]

    def user_contents(self):
        return [c["content"] for c in self.calls if c["role"] == "user"]


def _req(message="here is the link https://youtu.be/W2cLHN3bs8k"):
    r = m.ChatRequest(message=message)
    r.clean()
    return r


def _fake_request():
    class R:
        cookies = {}
        headers = {}

        class client:
            host = "test"
    return R()


def _spy_saves(monkeypatch):
    spy = _Spy()
    monkeypatch.setattr(m.crm, "save_turn", spy)
    monkeypatch.setattr(m, "_scoped_chat_id", lambda request, cid: "default")
    # run the background threads inline so assertions are deterministic
    monkeypatch.setattr(m.threading, "Thread",
                        lambda target, args=(), daemon=None: type(
                            "T", (), {"start": lambda _s: target(*args)})())
    return spy


def test_user_message_is_saved_before_the_model_runs(monkeypatch):
    spy = _spy_saves(monkeypatch)
    m._persist_user_message(_fake_request(), _req())
    assert spy.roles() == ["user"], "the question must be saved on arrival"
    assert "youtu.be/W2cLHN3bs8k" in spy.user_contents()[0]


def test_a_stopped_reply_still_keeps_the_question(monkeypatch):
    """THE BUG: stop used to discard the question along with the reply."""
    spy = _spy_saves(monkeypatch)
    req = _req()
    m._persist_user_message(_fake_request(), req)
    # ...user hits stop: the endpoints return early and never call _persist_turn
    assert "user" in spy.roles(), "the pasted link must survive a stop"
    assert "assistant" not in spy.roles(), "a stopped reply is not worth saving"


def test_persist_turn_no_longer_writes_the_user_half(monkeypatch):
    """Both halves in one call is what coupled them. Guard against a revert."""
    spy = _spy_saves(monkeypatch)
    result = m.ChatResponse(reply="done")
    m._persist_turn(_fake_request(), _req(), result)
    assert spy.roles() == ["assistant"], (
        "_persist_turn must save only the assistant half, or a stop loses the "
        "question again")


def test_a_completed_turn_saves_exactly_one_of_each(monkeypatch):
    spy = _spy_saves(monkeypatch)
    rq, fr = _req(), _fake_request()
    m._persist_user_message(fr, rq)
    m._persist_turn(fr, rq, m.ChatResponse(reply="the answer"))
    assert spy.roles() == ["user", "assistant"], "no duplicate user turn"


def test_blank_message_is_not_persisted(monkeypatch):
    spy = _spy_saves(monkeypatch)
    m._persist_user_message(_fake_request(), _req("   "))
    assert spy.calls == []
