"""Annabelle tools for the Dreamerie screens that had no chat path.

Pattern (same as draft_email / send_email in this app, now enforced in code):
  * a read answers at once;
  * a write returns a DRAFT and changes nothing; it runs only when the person's
    NEXT message says yes and the model then calls confirm_action with the
    draft id (once, within 5 minutes, never in the same turn as the draft);
  * every tool wraps the same function the screen's button calls;
  * keys, API tokens, app passwords, the access code and webhooks stay
    manual-only (Settings screen);
  * a password is never put into the chat and never emailed: Dreamerie has no
    invite or reset link flow, so add-user and reset-password are MANUAL-ONLY
    (Settings screen). The tools stay so Annabelle can point there; they
    draft nothing and change nothing.

main.py calls configure() once with the few functions that live in main.py and
note_session() before each owner turn.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from typing import Any, Callable, Dict, Optional

from . import brand, crm, results, users

TTL_SECONDS = 300
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

_deps: Dict[str, Any] = {}
# chat_id -> {draft_id: draft}
_pending: Dict[str, Dict[str, dict]] = {}
_superadmin_chats: Dict[str, bool] = {}


def configure(**fns: Any) -> None:
    _deps.update(fns)


def note_session(chat_id: str, superadmin: bool) -> None:
    _superadmin_chats[chat_id or "default"] = bool(superadmin)


def reset_state() -> None:
    _pending.clear()
    _superadmin_chats.clear()


def _tool(name: str, description: str, props: Optional[dict] = None, required: Optional[list] = None) -> dict:
    return {
        "name": name,
        "description": description,
        "input_schema": {"type": "object", "properties": props or {}, "required": required or []},
    }


_S = {"type": "string"}

TOOLS = [
    _tool("write_content",
          "Write platform-shaped copy for a topic (the Content screen's Write button). Saves nothing; show Susan the text and its numeric claims. To keep it, use draft_social_post.",
          {"topic": _S, "platform": _S, "tone": _S}, ["topic"]),
    _tool("list_strategies", "List the stored client strategies (names and priority, no content)."),
    _tool("get_client_strategy", "Read the stored strategy for one client.",
          {"client": _S, "kind": _S}, ["client"]),
    _tool("save_strategy",
          "DRAFT a client strategy save (replaces the same client and kind). Nothing is saved until Susan says yes and you call confirm_action.",
          {"client": _S, "content": _S, "kind": _S, "priority": {"type": "string", "enum": ["high", "normal", "low"]}},
          ["client", "content"]),
    _tool("get_brand", "Read the current brand colour and logo."),
    _tool("set_brand",
          "DRAFT a brand change (accent colour like #3f6f66 and/or logo URL). Nothing changes until Susan says yes and you call confirm_action.",
          {"accent": _S, "logo_url": _S}),
    _tool("get_settings",
          "Read the non-secret Settings: usage caps and counts, voice, automation level, default model, business websites, which connections are on. Never shows keys, passwords or the access code."),
    _tool("update_settings",
          "DRAFT a Settings change for caps (search_cap, public_chat_cap, owner_chat_cap), tts_voice, automation_level, chat_model_default, media_image_provider, or a business website (website_dreamerie, website_suzy_d, website_bear_arms, website_peptides). Keys, passwords, the access code and webhooks cannot be set here; send Susan to the Settings screen. Nothing changes until she says yes and you call confirm_action.",
          {"search_cap": _S, "public_chat_cap": _S, "owner_chat_cap": _S, "tts_voice": _S,
           "automation_level": _S, "chat_model_default": _S, "media_image_provider": _S,
           "website_dreamerie": _S, "website_suzy_d": _S, "website_bear_arms": _S, "website_peptides": _S}),
    _tool("list_team_users", "List the team logins (username, email, role, last login). Never shows passwords."),
    _tool("add_team_user",
          "MANUAL ONLY. Dreamerie has no invite link, so a new team login is added by hand on the Settings screen (the person sets their own password). Calling this changes nothing; it tells Susan where to go.",
          {"username": _S, "email": _S, "role": {"type": "string", "enum": ["owner", "staff"]}},
          ["username", "email"]),
    _tool("delete_team_user",
          "DRAFT deleting a team login. Cannot delete the main owner or the last owner. Nothing happens until Susan says yes and you call confirm_action.",
          {"username": _S}, ["username"]),
    _tool("reset_team_user_password",
          "MANUAL ONLY. Dreamerie has no reset link, so a password reset is done by hand on the Settings screen. Calling this changes nothing; it tells Susan where to go.",
          {"username": _S}, ["username"]),
    _tool("reset_customer_memory",
          "DRAFT wiping all customer data and client identity for a fresh handoff (logins and keys are kept). Super-admin only. Needs Susan to type the word RESET in her next message; pass it as typed to confirm_action.",
          {}),
    _tool("export_summary",
          "Count what an export would contain (conversations and users). The JSON file itself is a download from the Settings screen's Export button; give her that, not the data.",
          {}),
    _tool("get_results", "Read the monthly proof-of-value summary: leads, bookings, conversations. Month like 2026-10, blank for this month.",
          {"month": _S}),
    _tool("list_pending_requests", "List build requests still New or Building."),
    _tool("set_request_status",
          "DRAFT moving a build request to New, Building or Done. Nothing changes until Susan says yes and you call confirm_action.",
          {"req_id": _S, "status": {"type": "string", "enum": ["New", "Building", "Done"]}}, ["req_id", "status"]),
    _tool("confirm_action",
          "Run a held draft. ONLY after Susan has said yes in her own message AFTER the draft was shown; never in the same turn as the draft. One use, 5 minutes. For reset_customer_memory pass typed=RESET exactly as she typed it.",
          {"draft_id": _S, "typed": _S}, ["draft_id"]),
]
TOOL_NAMES = {t["name"] for t in TOOLS}
DRAFT_TOOLS = {"save_strategy", "set_brand", "update_settings", "delete_team_user",
               "reset_customer_memory", "set_request_status"}


# ---------------------------------------------------------------- drafts --

def _hold(chat_id: str, request_id: str, tool: str, args: dict, summary: str) -> str:
    now = time.time()
    bucket = _pending.setdefault(chat_id, {})
    for k in [k for k, d in bucket.items() if now - d["at"] > TTL_SECONDS]:
        bucket.pop(k, None)
    did = "d" + secrets.token_hex(4)
    bucket[did] = {"tool": tool, "args": args, "at": now, "request_id": request_id}
    return (f"DRAFT {did}: {summary}\nNothing has changed yet. Show this to Susan and wait for her yes in her next message, "
            f"then call confirm_action with draft_id {did}.")


def _json(resp: Any) -> dict:
    body = getattr(resp, "body", resp)
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8", "replace")
    return json.loads(body) if isinstance(body, str) else dict(body)


def _user_label(u: dict) -> str:
    return f"{u.get('username')} ({u.get('role') or 'owner'}, {u.get('email') or 'no email'})"


def _owners() -> list:
    return [u for u in users.list_users() if (u.get("role") or "owner") == "owner"]


def _superadmin_name() -> str:
    return os.environ.get("SUPERADMIN_USERNAME", "owner").strip()


_MANUAL_ADD = ("Adding a team login is done by hand on the Settings screen: Dreamerie has no invite link, "
               "and I will not email or type a password. Nothing was changed.")
_MANUAL_RESET = ("Resetting a password is done by hand on the Settings screen: Dreamerie has no reset link, "
                 "and I will not email or type a password. Nothing was changed.")


# ----------------------------------------------------------------- reads --

def _run_read(name: str, a: dict) -> str:
    if name == "write_content":
        topic = str(a.get("topic") or "").strip()
        if not topic:
            return "Give me a topic or angle to write about."
        mk = _deps["content_in"]
        r = _json(_deps["content_write"](mk(topic=topic, platform=str(a.get("platform") or ""), tone=str(a.get("tone") or ""))))
        if not r.get("ok"):
            return "I could not write that: " + str(r.get("error") or "unknown error")
        claims = r.get("claims") or []
        return ("Draft copy (nothing saved):\n" + str(r.get("content", "")) + "\n" + str(r.get("hashtags", "")) +
                ("\nNumbers to check before posting: " + "; ".join(map(str, claims)) if claims else "") +
                ("\nNeeds media: " + str(r["needs_media"]) if r.get("needs_media") else ""))
    if name == "list_strategies":
        rows = crm.list_strategies()
        return (f"{len(rows)} strategies: " + "; ".join(f"{r.get('client')} ({r.get('kind')}, {r.get('priority')})" for r in rows)) if rows else "No strategies are stored."
    if name == "get_client_strategy":
        client = str(a.get("client") or "").strip()
        if not client:
            return "Which client?"
        rows = crm.get_strategy(client, str(a.get("kind") or ""))
        if not rows:
            return f"No strategy is stored for {client}."
        return "\n\n".join(f"[{r.get('kind')}, {r.get('priority')}] {r.get('content')}" for r in rows)
    if name == "get_brand":
        b = brand.get_brand()
        return f"Brand colour {b.get('accent')}, logo {b.get('logo_url') or 'none'}."
    if name == "get_settings":
        return json.dumps(_deps["settings_read"](), default=str)
    if name == "list_team_users":
        rows = users.list_users()
        return (f"{len(rows)} logins: " + "; ".join(_user_label(u) + (f", last login {u['last_login']}" if u.get("last_login") else "") for u in rows)) if rows else "No team logins found."
    if name == "export_summary":
        n = len(crm.get_all_history(limit=9999))
        m = len(users.list_users())
        return (f"An export would hold {n} saved messages and {m} logins. The file is a download: Settings, then Export. "
                "I do not paste it into chat.")
    if name == "get_results":
        return json.dumps(results.get_monthly_summary(str(a.get("month") or "")), default=str)
    if name == "add_team_user":
        return _MANUAL_ADD
    if name == "reset_team_user_password":
        return _MANUAL_RESET
    if name == "list_pending_requests":
        rows = [r for r in crm.get_pending_requests() if r.get("status") in ("New", "Building")]
        return (f"{len(rows)} open build requests: " + "; ".join(f"{r.get('id') or r.get('record_id')}: {r.get('request') or r.get('title')} [{r.get('status')}]" for r in rows)) if rows else "No open build requests."
    return f"Unknown tool {name}."


# ---------------------------------------------------------------- drafting --

def _run_draft(name: str, a: dict, chat_id: str, request_id: str) -> str:
    def hold(args: dict, summary: str) -> str:
        return _hold(chat_id, request_id, name, args, summary)

    if name == "save_strategy":
        client, content = str(a.get("client") or "").strip(), str(a.get("content") or "").strip()
        if not client or not content:
            return "I need the client and the strategy text."
        kind, pri = str(a.get("kind") or "sales_strategy"), str(a.get("priority") or "normal")
        had = bool(crm.get_strategy(client, kind))
        return hold({"client": client, "content": content, "kind": kind, "priority": pri},
                    f"{'replace' if had else 'save'} the {kind} strategy for {client}, priority {pri} ({len(content)} characters).")
    if name == "set_brand":
        accent, logo = a.get("accent"), a.get("logo_url")
        if accent is None and logo is None:
            return "Tell me the new colour (like #3f6f66) or logo URL."
        if accent is not None and not _HEX_RE.match(str(accent).strip()):
            return "The colour must look like #3f6f66."
        cur = brand.get_brand()
        return hold({"accent": None if accent is None else str(accent).strip(), "logo_url": None if logo is None else str(logo).strip()},
                    f"brand changes from {cur.get('accent')} to {accent if accent is not None else 'the same colour'}"
                    + (f", logo to {logo}" if logo is not None else "") + ".")
    if name == "update_settings":
        opts = _deps["settings_choices"]()
        clean: Dict[str, str] = {}
        for k in ("search_cap", "public_chat_cap", "owner_chat_cap"):
            if a.get(k) not in (None, ""):
                try:
                    v = int(str(a[k]).strip())
                except ValueError:
                    return f"{k} must be a whole number."
                if v < 0:
                    return f"{k} cannot be negative."
                clean[k] = str(v)
        for k, allowed in (("tts_voice", opts["tts_voice"]), ("automation_level", opts["automation_level"]),
                           ("chat_model_default", opts["chat_model_default"]), ("media_image_provider", ("chatgpt", "grok"))):
            if a.get(k) not in (None, ""):
                if str(a[k]).strip() not in allowed:
                    return f"{k} must be one of: {', '.join(allowed)}."
                clean[k] = str(a[k]).strip()
        for b in opts["brands"]:
            k = f"website_{b}"
            if a.get(k) not in (None, ""):
                u = str(a[k]).strip()
                if " " in u or "." not in u:
                    return f"{k} must be a web address."
                clean[k] = u
        refused = [k for k in a if k not in clean and a.get(k) not in (None, "")]
        if refused:
            return "I cannot set " + ", ".join(refused) + " here. Keys, passwords, the access code and webhooks are set by hand on the Settings screen."
        if not clean:
            return "Tell me which setting to change."
        return hold(clean, "change settings: " + ", ".join(f"{k} to {v}" for k, v in clean.items()) + ".")
    if name == "set_request_status":
        rid, st = str(a.get("req_id") or "").strip(), str(a.get("status") or "").strip()
        if not rid or st not in ("New", "Building", "Done"):
            return "I need the request id and a status of New, Building or Done."
        return hold({"req_id": rid, "status": st}, f"move build request {rid} to {st}.")
    if name == "delete_team_user":
        uname = str(a.get("username") or "").strip()
        u = users.get_user(uname) if uname else None
        if not u:
            return f"No login named {uname or '(none given)'}."
        if uname == _superadmin_name():
            return f"{uname} is the main owner login and cannot be deleted here."
        if (u.get("role") or "owner") == "owner" and len(_owners()) <= 1:
            return "That is the last owner login and cannot be deleted."
        return hold({"username": uname}, f"delete the login {_user_label(u)}. Their chat history stays but they can no longer sign in.")
    if name == "reset_customer_memory":
        if not _superadmin_chats.get(chat_id or "default"):
            return "Only the super-admin login can wipe customer data. Nothing was changed."
        return hold({}, "WIPE all customer data and client identity for a fresh handoff. Logins and keys are kept. This cannot be undone. Susan must type the word RESET in her next message.")
    return f"Unknown tool {name}."


# ----------------------------------------------------------------- confirm --

def _apply(d: dict, chat_id: str, typed: str) -> str:
    t, a = d["tool"], d["args"]
    if t == "save_strategy":
        r = crm.save_strategy(client=a["client"], content=a["content"], kind=a["kind"], priority=a["priority"])
        return f"Strategy saved for {a['client']}." if r.get("ok") else "The strategy did not save: " + str(r.get("error") or r)
    if t == "set_brand":
        b = brand.set_brand(accent=a["accent"], logo_url=a["logo_url"])
        return f"Brand updated: colour {b.get('accent')}."
    if t == "update_settings":
        r = _json(_deps["apply_settings"](a))
        return "Settings saved." if r.get("ok") else "Not saved: " + str(r.get("detail") or r)
    if t == "set_request_status":
        return f"Request {a['req_id']} is now {a['status']}." if crm.update_request_status(a["req_id"], a["status"]) else "That did not save."
    if t == "delete_team_user":
        return f"Deleted the login {a['username']}." if users.delete_user(a["username"]) else "That login was not found."
    if t == "reset_customer_memory":
        if (typed or "").strip() != "RESET":
            return "Not done: Susan must type the word RESET. The draft is still waiting."
        if not _superadmin_chats.get(chat_id or "default"):
            return "Only the super-admin login can wipe customer data. Nothing was changed."
        s = crm.reset_customer_data()
        return "Customer data wiped. " + json.dumps(s, default=str) if s.get("ok") else "The wipe did not complete: " + json.dumps(s, default=str)
    return "Unknown draft."


def _confirm(a: dict, chat_id: str, request_id: str) -> str:
    did = str(a.get("draft_id") or "").strip()
    bucket = _pending.get(chat_id, {})
    d = bucket.get(did)
    if not d:
        return "No held draft with that id for this chat. It may have been used or expired; draft it again."
    if time.time() - d["at"] > TTL_SECONDS:
        bucket.pop(did, None)
        return "That draft expired after 5 minutes. Draft it again."
    if request_id and request_id == d["request_id"]:
        return "Not run: the draft and the yes cannot be in the same turn. Show Susan the draft and wait for her next message."
    if d["tool"] == "reset_customer_memory" and (str(a.get("typed") or "").strip() != "RESET"):
        return "Not run: Susan must type the word RESET. The draft is still waiting."
    bucket.pop(did, None)
    return _apply(d, chat_id, str(a.get("typed") or ""))


# ------------------------------------------------------------------- entry --

def run(name: str, args: dict, chat_id: str = "", request_id: str = "") -> str:
    chat_id = chat_id or "default"
    args = args or {}
    try:
        if name == "confirm_action":
            return _confirm(args, chat_id, request_id)
        if name in DRAFT_TOOLS:
            return _run_draft(name, args, chat_id, request_id)
        return _run_read(name, args)
    except Exception as e:  # a tool must answer, never raise into the chat loop
        return f"That did not work: {type(e).__name__}: {e}"
