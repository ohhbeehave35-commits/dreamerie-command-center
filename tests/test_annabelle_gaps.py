"""Annabelle gap tools: reads answer, writes are drafts that run only on the
person's NEXT-message yes, passwords never reach the chat, secrets stay manual.

Deterministic, no network, no model tokens.
"""

import types

import pytest

from app import annabelle_gaps as ag


@pytest.fixture(autouse=True)
def _clean():
    ag.reset_state()
    yield
    ag.reset_state()


class Store:
    """Fake of the app modules the tools wrap."""

    def __init__(self, monkeypatch):
        self.calls = []
        self.users = {"owner": {"username": "owner", "email": "o@x.com", "role": "owner"},
                      "nick": {"username": "nick", "email": "nick@x.com", "role": "staff"}}
        self.mail = []
        self.mail_ok = True
        self.strategies = {}
        self.brand = {"accent": "#111111", "logo_url": ""}
        self.reset_called = 0
        m = monkeypatch
        m.setattr(ag.crm, "save_strategy", lambda client, content, kind="sales_strategy", priority="normal": self._save_strategy(client, content, kind, priority))
        m.setattr(ag.crm, "get_strategy", lambda c, k="": [s for s in self.strategies.values() if s["client"] == c and (not k or s["kind"] == k)])
        m.setattr(ag.crm, "list_strategies", lambda: [dict(s, chars=len(s["content"])) for s in self.strategies.values()])
        m.setattr(ag.crm, "get_pending_requests", lambda: [{"id": "r1", "request": "Add sms", "details": "", "status": "New"}, {"id": "r2", "request": "Old", "details": "", "status": "Done"}])
        m.setattr(ag.crm, "update_request_status", lambda rid, st: self.calls.append(("status", rid, st)) or True)
        m.setattr(ag.crm, "get_all_history", lambda limit=9999: [1, 2, 3])
        m.setattr(ag.crm, "reset_customer_data", lambda: self.calls.append(("reset",)) or {"ok": True})
        m.setattr(ag.brand, "get_brand", lambda: dict(self.brand))
        m.setattr(ag.brand, "set_brand", lambda accent=None, logo_url=None: self._set_brand(accent, logo_url))
        m.setattr(ag.results, "get_monthly_summary", lambda month="": {"month": month or "now", "leads": 4})
        m.setattr(ag.users, "list_users", lambda: [dict(u) for u in self.users.values()])
        m.setattr(ag.users, "get_user", lambda n: dict(self.users[n]) if n in self.users else None)
        m.setattr(ag.users, "add_user", lambda n, e, p, r: self._add_user(n, e, p, r))
        m.setattr(ag.users, "delete_user", lambda n: self.users.pop(n, None) is not None)
        m.setattr(ag.users, "update_user_password", lambda n, p: self.calls.append(("pw", n, p)) or True)
        ag.configure(
            content_in=lambda **kw: types.SimpleNamespace(**kw),
            content_write=lambda body: types.SimpleNamespace(body=b'{"ok": true, "content": "Fresh candles", "hashtags": "#a", "claims": ["20% off"], "needs_media": ""}'),
            settings_read=lambda: {"search_cap": 10, "gmail_connected": True},
            settings_choices=lambda: {"tts_voice": ("v1",), "automation_level": ("manual", "semi_auto"), "chat_model_default": ("m1",), "brands": ("dreamerie", "suzy_d")},
            apply_settings=lambda f: self.calls.append(("settings", dict(f))) or types.SimpleNamespace(body=b'{"ok": true}'),
            after_user_added=lambda n: self.calls.append(("defaults", n)),
        )

    def _save_strategy(self, client, content, kind, priority):
        self.calls.append(("strategy", client))
        self.strategies[(client, kind)] = {"client": client, "kind": kind, "priority": priority, "content": content}
        return {"ok": True}

    def _set_brand(self, accent, logo):
        self.calls.append(("brand", accent, logo))
        if accent is not None:
            self.brand["accent"] = accent
        return dict(self.brand)

    def _add_user(self, n, e, p, r):
        self.calls.append(("add", n, e, r))
        self.users[n] = {"username": n, "email": e, "role": r}
        return True, ""

    def _send(self, to, subj, body):
        self.mail.append((to, subj, body))
        return f"Sent to {to} from x." if self.mail_ok else "Couldn't send that email: boom"


@pytest.fixture
def st(monkeypatch):
    return Store(monkeypatch)


def draft_id(text):
    return text.split("DRAFT ", 1)[1].split(":", 1)[0]


def test_reads_answer_at_once_and_change_nothing(st):
    assert "Fresh candles" in ag.run("write_content", {"topic": "fall"}, "c", "t1")
    assert "20% off" in ag.run("write_content", {"topic": "fall"}, "c", "t1")
    assert "No strategies" in ag.run("list_strategies", {}, "c", "t1")
    assert "#111111" in ag.run("get_brand", {}, "c", "t1")
    assert "leads" in ag.run("get_results", {"month": "2026-10"}, "c", "t1")
    assert "Add sms" in ag.run("list_pending_requests", {}, "c", "t1") and "Old" not in ag.run("list_pending_requests", {}, "c", "t1")
    assert "nick" in ag.run("list_team_users", {}, "c", "t1")
    assert "3 saved messages" in ag.run("export_summary", {}, "c", "t1")
    assert "search_cap" in ag.run("get_settings", {}, "c", "t1")
    assert st.calls == []


def test_write_is_a_draft_and_runs_only_on_the_next_turn(st):
    d = ag.run("save_strategy", {"client": "Acme", "content": "Call Tuesday"}, "c", "turn1")
    assert d.startswith("DRAFT") and st.calls == []
    # same turn: refused, nothing saved
    assert "same turn" in ag.run("confirm_action", {"draft_id": draft_id(d)}, "c", "turn1")
    assert st.calls == []
    # her yes arrives in the next turn
    assert "saved" in ag.run("confirm_action", {"draft_id": draft_id(d)}, "c", "turn2")
    assert st.calls == [("strategy", "Acme")]
    # once only
    assert "No held draft" in ag.run("confirm_action", {"draft_id": draft_id(d)}, "c", "turn3")
    assert st.calls == [("strategy", "Acme")]


def test_draft_belongs_to_one_chat_and_expires(st, monkeypatch):
    d = ag.run("set_brand", {"accent": "#3f6f66"}, "chatA", "t1")
    assert "No held draft" in ag.run("confirm_action", {"draft_id": draft_id(d)}, "chatB", "t2")
    assert st.calls == []
    real = ag.time.time
    monkeypatch.setattr(ag.time, "time", lambda: real() + ag.TTL_SECONDS + 5)
    assert "expired" in ag.run("confirm_action", {"draft_id": draft_id(d)}, "chatA", "t2")
    assert st.calls == []


def test_brand_validates_before_drafting(st):
    assert "must look like" in ag.run("set_brand", {"accent": "red"}, "c", "t1")
    d = ag.run("set_brand", {"accent": "#3f6f66"}, "c", "t1")
    ag.run("confirm_action", {"draft_id": draft_id(d)}, "c", "t2")
    assert st.brand["accent"] == "#3f6f66"


def test_settings_changes_are_whitelisted_and_secrets_stay_manual(st):
    out = ag.run("update_settings", {"gmail_app_password": "x" * 16}, "c", "t1")
    assert "by hand on the Settings screen" in out and st.calls == []
    assert "one of" in ag.run("update_settings", {"automation_level": "yolo"}, "c", "t1")
    assert "whole number" in ag.run("update_settings", {"search_cap": "lots"}, "c", "t1")
    d = ag.run("update_settings", {"search_cap": "50", "website_dreamerie": "https://d.example.com"}, "c", "t1")
    assert st.calls == []
    ag.run("confirm_action", {"draft_id": draft_id(d)}, "c", "t2")
    assert st.calls == [("settings", {"search_cap": "50", "website_dreamerie": "https://d.example.com"})]
    for secret_field in ("access_code", "xai_api_key", "openai_api_key", "elevenlabs_api_key", "zapier_webhook_url"):
        assert "by hand" in ag.run("update_settings", {secret_field: "s"}, "c", "t3")


def test_no_tool_emails_a_password():
    assert not hasattr(ag, "_send_temp_password")
    assert not hasattr(ag, "password_reset")


def test_delete_user_protects_the_main_owner_and_last_owner(st):
    assert "main owner" in ag.run("delete_team_user", {"username": "owner"}, "c", "t1")
    st.users["boss"] = {"username": "boss", "email": "b@x.com", "role": "owner"}
    del st.users["owner"]
    assert "last owner" in ag.run("delete_team_user", {"username": "boss"}, "c", "t1")
    d = ag.run("delete_team_user", {"username": "nick"}, "c", "t1")
    assert "nick" in st.users
    ag.run("confirm_action", {"draft_id": draft_id(d)}, "c", "t2")
    assert "nick" not in st.users


def test_reset_memory_needs_super_admin_the_typed_word_and_the_next_turn(st):
    assert "super-admin" in ag.run("reset_customer_memory", {}, "c", "t1")
    ag.note_session("c", True)
    d = ag.run("reset_customer_memory", {}, "c", "t1")
    assert "RESET" in d and st.calls == []
    assert "RESET" in ag.run("confirm_action", {"draft_id": draft_id(d)}, "c", "t2")
    assert "RESET" in ag.run("confirm_action", {"draft_id": draft_id(d), "typed": "yes"}, "c", "t2")
    assert st.calls == []
    assert "wiped" in ag.run("confirm_action", {"draft_id": draft_id(d), "typed": "RESET"}, "c", "t3")
    assert st.calls == [("reset",)]


def test_request_status_draft_then_yes(st):
    d = ag.run("set_request_status", {"req_id": "r1", "status": "Done"}, "c", "t1")
    assert st.calls == []
    ag.run("confirm_action", {"draft_id": draft_id(d)}, "c", "t2")
    assert st.calls == [("status", "r1", "Done")]


def test_tools_are_offered_in_every_mode_and_never_to_the_public():
    from app.agents import DELEGATION_TOOLS, MODE_TOOLS, PUBLIC_TOOLS
    names = {t["name"] for t in DELEGATION_TOOLS}
    assert ag.TOOL_NAMES <= names
    for mode, allowed in MODE_TOOLS.items():
        assert ag.TOOL_NAMES <= allowed, mode
    assert not (ag.TOOL_NAMES & {t["name"] for t in PUBLIC_TOOLS})


def test_chat_dispatch_reaches_the_draft_and_changes_nothing(monkeypatch):
    import app.main as main
    st = Store(monkeypatch)
    monkeypatch.setattr(main.crm, "get_chat_count", lambda persona: 0)
    monkeypatch.setattr(main.crm, "increment_chat_count", lambda *a, **k: 0)

    def tb(name, inp):
        return types.SimpleNamespace(type="tool_use", name=name, input=inp, id="tu1")

    class S:
        def __init__(self, r):
            self.r = r
            self.text_stream = iter([b.text for b in r.content if b.type == "text"])
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get_final_message(self): return self.r

    script = [types.SimpleNamespace(content=[tb("set_brand", {"accent": "#3f6f66"})], stop_reason="tool_use"),
              types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text="Draft ready.")], stop_reason="end_turn")]
    fake = types.SimpleNamespace(messages=types.SimpleNamespace(stream=lambda **k: S(script.pop(0))))
    monkeypatch.setattr(main, "client", fake)
    from app.agents import DELEGATION_TOOLS
    resp = main.run_main_brain("make the brand green", [], "sys", DELEGATION_TOOLS, persona="owner", chat_id="c9", request_id="r9")
    assert "Owner tools" in resp.delegated_to
    assert st.brand["accent"] == "#111111" and st.calls == []
    assert any(d["tool"] == "set_brand" for d in ag._pending["c9"].values())


def test_every_gap_tool_has_a_dispatch_branch_in_main():
    import re
    from pathlib import Path
    src = Path(__file__).resolve().parent.parent.joinpath("app", "main.py").read_text(encoding="utf-8")
    handled = set()
    for grp in re.findall(r'block\.name\s+in\s*\(([^)]*)\)', src):
        handled |= set(re.findall(r'"([a-z0-9_]+)"', grp))
    assert ag.TOOL_NAMES <= handled


# ---- ORDER WIRE1 (Director P2, 30 Oct 2026 23:58 ET): the four screen actions that had no chat path ----

WIRE1_NAMES = {"start_new_chat", "read_chat_history", "calendar_upcoming", "push_hubspot_lead"}


class Wire1Store:
    def __init__(self, monkeypatch):
        self.calls = []
        self.history = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi Susan"}]
        self.events = {"connected": True, "events": [{"date": "2026-11-02", "time": "9:00 AM", "title": "Candle pour", "kind": "other"}]}
        m = monkeypatch
        m.setattr(ag.crm, "get_history", lambda limit=40, chat_id="default": self.calls.append(("history", limit, chat_id)) or list(self.history))
        m.setattr(ag.gcal, "list_upcoming_events", lambda days=30, business="": self.calls.append(("cal", days)) or self.events)
        ag.configure(
            start_chat=lambda name, owner: self.calls.append(("chat", name, owner)) or "abc123",
            hubspot_push=lambda f: self.calls.append(("hubspot", dict(f))) or types.SimpleNamespace(
                body=b'{"ok": true, "contact_id": "c1", "deal_id": "d1"}'),
        )


@pytest.fixture
def w1(monkeypatch):
    return Wire1Store(monkeypatch)


def test_wire1_names_match_the_hand_controls():
    assert WIRE1_NAMES <= ag.TOOL_NAMES
    assert "push_hubspot_lead" in ag.DRAFT_TOOLS
    assert not ({"start_new_chat", "read_chat_history", "calendar_upcoming"} & ag.DRAFT_TOOLS)


def test_start_new_chat_creates_a_chat_for_the_signed_in_person(w1):
    ag.note_session("user:nick:c", False, scope="user:nick")
    out = ag.run("start_new_chat", {"name": "Fall launch"}, "user:nick:c", "t1")
    assert "abc123" in out and "Fall launch" in out
    assert w1.calls == [("chat", "Fall launch", "nick")]


def test_start_new_chat_on_the_shared_access_code_uses_the_shared_owner(w1):
    ag.note_session("access:c", False, scope="access")
    ag.run("start_new_chat", {}, "access:c", "t1")
    assert w1.calls == [("chat", "New Chat", "shared")]


def test_read_chat_history_reads_this_chat_and_changes_nothing(w1):
    ag.note_session("user:nick:c", False, scope="user:nick")
    out = ag.run("read_chat_history", {"limit": 5}, "user:nick:c", "t1")
    assert "hello" in out and "hi Susan" in out
    assert w1.calls == [("history", 5, "user:nick:c")]


def test_read_chat_history_of_another_chat_stays_inside_the_persons_own_scope(w1):
    ag.note_session("user:nick:c", False, scope="user:nick")
    ag.run("read_chat_history", {"chat_id": "zz9"}, "user:nick:c", "t1")
    assert w1.calls == [("history", 20, "user:nick:zz9")]


def test_read_chat_history_refuses_to_guess_whose_chat_when_scope_unknown(w1):
    out = ag.run("read_chat_history", {"chat_id": "zz9"}, "c", "t1")
    assert "sign in" in out.lower() and w1.calls == []


def test_calendar_upcoming_answers_at_once(w1):
    out = ag.run("calendar_upcoming", {"days": 14}, "c", "t1")
    assert "Candle pour" in out and "2026-11-02" in out
    assert w1.calls == [("cal", 14)]


def test_calendar_upcoming_says_when_the_calendar_is_not_connected(w1):
    w1.events = {"connected": False, "events": []}
    assert "not connected" in ag.run("calendar_upcoming", {}, "c", "t1").lower()


def test_push_hubspot_lead_is_a_draft_and_runs_once_on_the_next_turn(w1):
    d = ag.run("push_hubspot_lead", {"name": "Pat Lee", "email": "pat@x.com", "notes": "wants 20 candles"}, "c", "turn1")
    assert d.startswith("DRAFT") and "Pat Lee" in d and w1.calls == []
    assert "same turn" in ag.run("confirm_action", {"draft_id": draft_id(d)}, "c", "turn1")
    assert w1.calls == []
    out = ag.run("confirm_action", {"draft_id": draft_id(d)}, "c", "turn2")
    assert "HubSpot" in out and "c1" in out
    assert w1.calls == [("hubspot", {"name": "Pat Lee", "email": "pat@x.com", "phone": "", "company": "", "service": "", "notes": "wants 20 candles"})]
    ag.run("confirm_action", {"draft_id": draft_id(d)}, "c", "turn3")
    assert len(w1.calls) == 1


def test_push_hubspot_lead_needs_a_name_and_a_plausible_email(w1):
    assert "name" in ag.run("push_hubspot_lead", {}, "c", "t1").lower()
    assert "email" in ag.run("push_hubspot_lead", {"name": "Pat", "email": "nope"}, "c", "t1").lower()
    assert w1.calls == [] and not ag._pending.get("c")


def test_push_hubspot_lead_reports_a_hubspot_failure_honestly(w1, monkeypatch):
    ag.configure(hubspot_push=lambda f: types.SimpleNamespace(body=b'{"ok": false, "detail": "HubSpot not configured"}'))
    d = ag.run("push_hubspot_lead", {"name": "Pat"}, "c", "a")
    out = ag.run("confirm_action", {"draft_id": draft_id(d)}, "c", "b")
    assert "not configured" in out and "pushed" not in out.lower()


def test_wire1_tools_run_the_same_functions_the_buttons_run(monkeypatch):
    """No second copy of the logic: the endpoints and the tools both call main's _start_new_chat / _push_hubspot_lead."""
    import asyncio, inspect
    import app.main as main
    made = []
    monkeypatch.setattr(main.crm, "create_chat_session", lambda name, owner: made.append((name, owner)) or "zz")
    assert main._start_new_chat("A", "nick") == "zz" and made == [("A", "nick")]
    sent = []
    monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", "test")
    monkeypatch.setattr(main.hubspot, "capture_lead", lambda **kw: sent.append(kw) or {"contact": {"id": "c"}, "deal": {"id": "d"}})
    r = main.hubspot_lead(main.HubSpotLeadRequest(name="Pat"))   # the endpoint (what a hand call runs)
    r2 = main._push_hubspot_lead(main.HubSpotLeadRequest(name="Pat"))   # what the tool runs after the yes
    assert r.body == r2.body and len(sent) == 2
    assert "_push_hubspot_lead(" in inspect.getsource(main.hubspot_lead)
    assert "_start_new_chat(" in inspect.getsource(main.create_chat)
    # and the tool wiring in main.configure(...) points at them
    src = inspect.getsource(main)
    assert "start_chat=lambda name, owner: _start_new_chat" in src
    assert "hubspot_push=lambda fields: _push_hubspot_lead" in src
