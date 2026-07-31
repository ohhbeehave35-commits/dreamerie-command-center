"""
TOOL ROUND CAP HONESTY -- regression for the 30 Jul P1 fake-save.

The live browser sweep caught Annabelle saying "Saved --" three consecutive
times with no save_memory call. Root cause (confirmed by code reading): the
tool loop was capped at 4 model rounds, and on exhaustion the accumulated
narration -- including claims about work planned for the round that never
came -- shipped verbatim as the final reply. The intended save was starved,
and nothing ever told the model (or the user) that it didn't run.

The fix under test:
  * TOOL_ROUND_CAP = 8 (recall -> save -> confirm plus other tools fits).
  * On exhaustion while the model still wants tools, ONE final TOOL-LESS
    wrap-up call is made, carrying ROUND_CAP_WRAPUP_NOTE ("any tool call
    without a tool_result DID NOT RUN"), so the reply is composed from what
    actually happened instead of stale narration.

Deterministic, mocked model, zero tokens. This file is INCLUDED in the
pre-push gate (it matches no exclusion pattern) on purpose.
"""

import json
import sys
import types

import pytest

sys.path.insert(0, ".")


class RoundCapDishonesty(AssertionError): ...
class RoundCapStarvation(AssertionError): ...


def _text_block(text):
    return types.SimpleNamespace(type="text", text=text)


def _tool_block(name, tool_input=None, block_id="tu_1"):
    return types.SimpleNamespace(type="tool_use", name=name,
                                 input=tool_input or {}, id=block_id)


def _resp(*blocks, stop_reason="end_turn"):
    return types.SimpleNamespace(content=list(blocks), stop_reason=stop_reason)


class _FakeStream:
    def __init__(self, resp):
        self._resp = resp
        self.text_stream = iter([b.text for b in resp.content if b.type == "text"])

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self._resp


class FakeAnthropic:
    """Plays a script of responses; repeats the last entry when it runs out.
    Records every call's kwargs so tests can assert on what the model SAW."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = []
        self.messages = types.SimpleNamespace(stream=self._stream)

    def _stream(self, **kwargs):
        self.calls.append(kwargs)
        resp = self._script.pop(0) if len(self._script) > 1 else self._script[0]
        return _FakeStream(resp)


@pytest.fixture
def drive(monkeypatch):
    import app.main as main

    monkeypatch.setattr(main.crm, "get_chat_count", lambda persona: 0)
    monkeypatch.setattr(main.crm, "increment_chat_count", lambda *a, **k: 0)
    monkeypatch.setattr(main.memory, "recall_memory",
                        lambda *a, **k: "Nothing on file for that yet.")
    monkeypatch.setattr(main.memory, "add_memory",
                        lambda *a, **k: "Memory saved.")

    from app.agents import DELEGATION_TOOLS

    def _drive(script, message="remember that the gate code is 4411"):
        fake = FakeAnthropic(script)
        monkeypatch.setattr(main, "client", fake)
        resp = main.run_main_brain(message, [], "You are a test system prompt.",
                                   DELEGATION_TOOLS, enable_search=False,
                                   persona="owner")
        return resp, fake

    return _drive


def _blob(call):
    return json.dumps(call.get("messages", []),
                      default=lambda o: getattr(o, "__dict__", str(o)))


def test_exhaustion_triggers_one_toolless_wrapup_call(drive):
    """Infinite tool_use must produce TOOL_ROUND_CAP tool rounds plus exactly
    one wrap-up call that (a) offers NO tools and (b) carries the DID-NOT-RUN
    note -- so the model cannot keep calling tools and cannot honestly claim
    the starved action ran."""
    import app.main as main
    resp, fake = drive([
        _resp(_tool_block("recall_memory", {"query": "gate code"}),
              stop_reason="tool_use"),
    ])
    n_expected = main.TOOL_ROUND_CAP + 1
    if len(fake.calls) != n_expected:
        raise RoundCapStarvation(
            f"expected {main.TOOL_ROUND_CAP} tool rounds + 1 wrap-up call = "
            f"{n_expected} model calls, saw {len(fake.calls)} -- either the cap "
            f"drifted or the wrap-up round is missing")
    wrapup = fake.calls[-1]
    if wrapup.get("tools"):
        raise RoundCapDishonesty(
            "the wrap-up call still offered tools -- the model can ask for a "
            "9th round instead of composing an honest final answer")
    if "DID NOT RUN" not in _blob(wrapup):
        raise RoundCapDishonesty(
            "the wrap-up call does not carry ROUND_CAP_WRAPUP_NOTE -- the model "
            "is composing a final answer without being told the starved tool "
            "calls never executed; stale narration will ship as the reply")


def test_wrapup_text_reaches_the_reply(drive):
    """The wrap-up call's honest text must be part of what the user reads,
    not swallowed by the stuck-text fallback."""
    import app.main as main
    honest = "I checked memory but the save itself did not run -- want me to retry?"
    script = [_resp(_text_block("Let me check that. "),
                    _tool_block("recall_memory", {"query": "gate code"}),
                    stop_reason="tool_use")
              for _ in range(main.TOOL_ROUND_CAP)]
    script.append(_resp(_text_block(honest)))
    resp, fake = drive(script)
    if honest not in resp.reply:
        raise RoundCapDishonesty(
            f"the wrap-up call's text never reached the reply; got: {resp.reply[:200]!r}")


def test_recall_save_confirm_chain_fits_under_the_cap(drive):
    """The exact flow the old cap starved: recall -> save -> spoken
    confirmation. It must complete with the save DISPATCHED and no wrap-up
    round -- guards against anyone lowering the cap below real flows again."""
    resp, fake = drive([
        _resp(_text_block("Checking what I have. "),
              _tool_block("recall_memory", {"query": "gate code"}),
              stop_reason="tool_use"),
        _resp(_tool_block("save_memory", {"summary": "Gate code",
                                          "content": "4411"}, block_id="tu_2"),
              stop_reason="tool_use"),
        _resp(_text_block("Saved it for real this time.")),
    ])
    if resp.delegated_to.count("Memory") != 2:
        raise RoundCapStarvation(
            f"recall+save should be 2 Memory dispatches, saw "
            f"{resp.delegated_to.count('Memory')} in {resp.delegated_to} -- the "
            f"save was starved or double-fired")
    assert "Saved it for real this time." in resp.reply
    if any("DID NOT RUN" in _blob(c) for c in fake.calls):
        raise RoundCapStarvation(
            "a 3-round chain hit the wrap-up note -- the cap is set below "
            "legitimate flows and will starve saves again")
