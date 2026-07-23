"""
FastAPI backend for The Dreamerie / Suzy D command center (Susan's business).
Built from the Stinger Industries delivery playbook.

Run with:
    uvicorn app.main:app --reload --port 8000

Requires ANTHROPIC_API_KEY set in the environment (see .env.example).
"""

import os
import threading
import time
from typing import List, Dict

import edge_tts
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response, HTMLResponse, JSONResponse
from pydantic import BaseModel
from anthropic import Anthropic

from . import crm
from . import emailer
from . import social
from . import assets
from . import events
from . import users
from .agents import (
    build_main_brain_prompt,
    build_public_prompt,
    SUB_AGENTS,
    DELEGATION_TOOLS,
    TOOL_NAME_TO_AGENT_KEY,
    PUBLIC_TOOLS,
)

AGENT_NAME_KEY = "agent_name"  # Airtable Settings key for Susan's chosen name

load_dotenv(override=True)  # .env wins over any stale system-level key

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5")
# Neural voice for spoken replies (free via Edge TTS). Try e.g. en-US-AndrewNeural,
# en-US-EmmaNeural, en-GB-SoniaNeural. Override with TTS_VOICE in .env.
TTS_VOICE = os.environ.get("TTS_VOICE", "en-GB-RyanNeural")
# Speaking pace, e.g. "-8%" for calmer/warmer, "+0%" default. Tune via env.
TTS_RATE = os.environ.get("TTS_RATE", "-6%")
# Grok (xAI) TTS: set XAI_API_KEY to use it; otherwise free Edge TTS is used.
# Voices: ara, eve, leo, rex, sal -- or a cloned voice_id.
XAI_API_KEY = os.environ.get("XAI_API_KEY", "")
XAI_VOICE = os.environ.get("XAI_VOICE", "eve")
client = Anthropic()  # reads ANTHROPIC_API_KEY from env

# Owner-only, metered live web search. Never added to PUBLIC_TOOLS -- the
# customer-facing widget can never trigger a search. Cap resets monthly (see
# crm._search_usage_key); once hit, the assistant is told to say so plainly.
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search"}
SEARCH_MONTHLY_CAP = int(os.environ.get("SEARCH_MONTHLY_CAP", "50"))
SEARCH_CAPPED_NOTE = (
    "\n\nNOTE: the web search budget for this billing period has been reached. "
    "If asked to search, do not attempt it -- tell Susan plainly that search is "
    "capped for now and Vinny needs to raise SEARCH_MONTHLY_CAP or wait for "
    "next month's reset."
)

# Platform-wide spend guardrail: a hard monthly cap on ordinary chat turns,
# checked BEFORE any Anthropic call is made. Public gets a much lower default
# than owner, since it's the surface a stranger or a bot can hit freely.
PUBLIC_MONTHLY_CAP = int(os.environ.get("PUBLIC_MONTHLY_CAP", "300"))
OWNER_MONTHLY_CAP = int(os.environ.get("OWNER_MONTHLY_CAP", "2000"))
PUBLIC_CAPPED_REPLY = (
    "Thanks so much for reaching out! Our assistant is temporarily at capacity "
    "for the moment -- please reach out directly and we'll get right back to "
    "you, or feel free to try again a bit later."
)
OWNER_CAPPED_REPLY = (
    "I've hit my chat budget for this billing period, Susan -- Vinny will need "
    "to raise OWNER_MONTHLY_CAP in the environment settings or wait for next "
    "month's reset."
)


def _int_override(key: str, default: int) -> int:
    """Read an admin-editable int setting from Airtable, falling back to the
    env-var default if unset or invalid."""
    try:
        raw = crm.get_setting(key, "")
        return int(raw) if raw else default
    except ValueError:
        return default


# Admin-editable overrides (Settings panel) win over env vars, take effect
# immediately -- no redeploy. Functions, not constants, so every request
# sees the latest value.
def get_search_cap() -> int:
    return _int_override("cap_search_monthly", SEARCH_MONTHLY_CAP)


def get_public_chat_cap() -> int:
    return _int_override("cap_chat_public", PUBLIC_MONTHLY_CAP)


def get_owner_chat_cap() -> int:
    return _int_override("cap_chat_owner", OWNER_MONTHLY_CAP)


def get_tts_voice() -> str:
    return crm.get_setting("tts_voice_override", "") or TTS_VOICE


def get_access_code() -> str:
    return crm.get_setting("access_code_override", "") or ACCESS_CODE


app = FastAPI(title="The Dreamerie Command Center")
ALLOWED_ORIGINS = [
    "https://dreamerie-command-center.onrender.com",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- Access gate (hosted deployments) -------------------------------------
# Set ACCESS_CODE in the environment to require a code before anything loads.
# Leave it unset for local use (localhost stays open).
ACCESS_CODE = os.environ.get("ACCESS_CODE", "")

LOCK_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>The Dreamerie Command Center</title></head>
<body style="margin:0;height:100vh;display:flex;align-items:center;justify-content:center;background:radial-gradient(ellipse at 50% 44%,#1a1224,#0a0710 74%);font-family:Inter,-apple-system,sans-serif">
<form id="f" style="display:flex;flex-direction:column;gap:14px;align-items:center;padding:36px 40px;background:rgba(20,14,26,0.85);border:1px solid rgba(196,150,230,0.35);border-radius:16px">
<div style="font-weight:700;font-size:19px;letter-spacing:0.16em;background:linear-gradient(180deg,#e0b8f0,#b87ad9);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent">THE DREAMERIE</div>
<div style="font-size:11px;letter-spacing:0.4em;color:#c8ccd2">COMMAND CENTER</div>
<input id="c" type="password" placeholder="Access code" autocomplete="current-password" style="margin-top:10px;padding:11px 14px;font-size:16px;width:220px;color:#e6e2d6;background:rgba(26,18,34,0.9);border:1px solid rgba(200,204,210,0.2);border-radius:10px;text-align:center">
<button style="padding:10px 26px;font-weight:600;font-size:14px;background:linear-gradient(180deg,#d9a8ec,#b87ad9);color:#241530;border:none;border-radius:10px;cursor:pointer">Unlock</button>
<div id="m" style="font-size:12px;color:#e0a48f;min-height:16px"></div>
</form>
<script>
document.getElementById('f').addEventListener('submit', async (e) => {
  e.preventDefault();
  const r = await fetch('/api/unlock', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code: document.getElementById('c').value }) });
  if (r.ok) { location.reload(); return; }
  const j = await r.json().catch(() => ({}));
  document.getElementById('m').textContent = j.detail || 'Incorrect code';
});
</script></body></html>"""


@app.middleware("http")
async def access_gate(request: Request, call_next):
    if not ACCESS_CODE:
        return await call_next(request)
    # Public, customer-facing paths (the website chat widget) are never gated.
    if request.url.path in ("/api/unlock", "/api/public-chat", "/widget", "/privacy"):
        return await call_next(request)
    if request.cookies.get("cc_access") == get_access_code():
        return await call_next(request)
    # Also accept a valid session token (multi-user login)
    session_tok = request.cookies.get("cc_session")
    if session_tok and users.verify_session_token(session_tok):
        return await call_next(request)
    if request.url.path.startswith("/api/") or request.url.path.startswith("/static/"):
        return JSONResponse({"detail": "locked"}, status_code=401)
    return HTMLResponse(LOCK_PAGE, status_code=401)


class UnlockRequest(BaseModel):
    code: str


class LoginRequest(BaseModel):
    username: str
    password: str


# Brute-force protection on the access gate: per-IP sliding-window lockout.
# In-memory (resets on redeploy/restart) -- fine for a single-instance app;
# the goal is defeating a simple automated guesser, not surviving a restart.
UNLOCK_MAX_ATTEMPTS = int(os.environ.get("UNLOCK_MAX_ATTEMPTS", "5"))
UNLOCK_WINDOW_SECONDS = int(os.environ.get("UNLOCK_WINDOW_SECONDS", "900"))  # 15 min
_unlock_attempts: Dict[str, list] = {}


def _client_ip(request: Request) -> str:
    # Render sits behind a proxy; X-Forwarded-For carries the real client IP first.
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.post("/api/unlock")
def unlock(req: UnlockRequest, request: Request) -> JSONResponse:
    ip = _client_ip(request)
    now = time.time()
    attempts = [t for t in _unlock_attempts.get(ip, []) if now - t < UNLOCK_WINDOW_SECONDS]
    if len(attempts) >= UNLOCK_MAX_ATTEMPTS:
        wait_min = max(1, int((UNLOCK_WINDOW_SECONDS - (now - attempts[0])) / 60) + 1)
        return JSONResponse(
            {"ok": False, "detail": f"Too many attempts. Try again in about {wait_min} minute(s)."},
            status_code=429,
        )
    effective_code = get_access_code()
    if effective_code and req.code == effective_code:
        _unlock_attempts.pop(ip, None)
        resp = JSONResponse({"ok": True})
        resp.set_cookie("cc_access", effective_code, max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax")
        return resp
    attempts.append(now)
    _unlock_attempts[ip] = attempts
    return JSONResponse({"ok": False}, status_code=401)


@app.post("/api/setup")
def setup_first_user(req: LoginRequest) -> JSONResponse:
    """One-time bootstrap: create the first admin user if no users exist yet.
    Disabled automatically once any user has been created."""
    existing = users.list_users()
    if existing:
        return JSONResponse(
            {"ok": False, "detail": "Setup already complete. Use /api/login."},
            status_code=403,
        )
    if not req.username or not req.password:
        return JSONResponse({"ok": False, "detail": "Username and password required"}, status_code=400)
    ok = users.add_user(req.username, "admin@dreamerie.com", req.password, role="owner")
    if not ok:
        return JSONResponse({"ok": False, "detail": "Failed to create user"}, status_code=500)
    token = users.create_session_token(req.username)
    resp = JSONResponse({"ok": True, "detail": f"Account created for {req.username}. You are now logged in."})
    resp.set_cookie("cc_session", token, max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax", secure=True)
    return resp


@app.post("/api/login")
def login(req: LoginRequest, request: Request) -> JSONResponse:
    """Authenticate with username + password, return signed session token in cookie."""
    ip = _client_ip(request)
    now = time.time()
    attempts = [t for t in _unlock_attempts.get(ip, []) if now - t < UNLOCK_WINDOW_SECONDS]
    if len(attempts) >= UNLOCK_MAX_ATTEMPTS:
        wait_min = max(1, int((UNLOCK_WINDOW_SECONDS - (now - attempts[0])) / 60) + 1)
        return JSONResponse(
            {"ok": False, "detail": f"Too many attempts. Try again in about {wait_min} minute(s)."},
            status_code=429,
        )
    user = users.get_user(req.username)
    if not user or not users.verify_password(req.password, user["password_hash"]):
        attempts.append(now)
        _unlock_attempts[ip] = attempts
        return JSONResponse({"ok": False, "detail": "Invalid username or password"}, status_code=401)
    _unlock_attempts.pop(ip, None)
    users.update_last_login(req.username)
    token = users.create_session_token(req.username)
    resp = JSONResponse({"ok": True})
    resp.set_cookie("cc_session", token, max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax", secure=True)
    return resp


@app.post("/api/logout")
def logout(request: Request) -> JSONResponse:
    """Clear session cookie."""
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("cc_session")
    resp.delete_cookie("cc_access")
    return resp


@app.get("/api/me")
def get_current_user(request: Request) -> JSONResponse:
    """Return info about the currently logged-in user."""
    session_token = request.cookies.get("cc_session")
    if not session_token:
        return JSONResponse({"username": None}, status_code=401)
    username = users.verify_session_token(session_token)
    if not username:
        return JSONResponse({"username": None}, status_code=401)
    user = users.get_user(username)
    if not user:
        return JSONResponse({"username": None}, status_code=401)
    return JSONResponse({
        "ok": True,
        "username": user["username"],
        "email": user["email"],
        "role": user["role"],
        "created_at": user["created_at"],
        "last_login": user["last_login"],
    })


class ChatRequest(BaseModel):
    message: str
    history: List[Dict[str, str]] = []  # [{"role": "user"|"assistant", "content": "..."}]


class ChatResponse(BaseModel):
    reply: str
    delegated_to: List[str] = []
    # Per-stage timing breakdown in seconds, ported from the flagship's
    # latency fix -- lets us verify the fix here with real numbers too.
    timings: Dict[str, float] = {}


def call_sub_agent(agent_key: str, query: str) -> str:
    """Run one sub-agent with a fresh, isolated context and return its answer."""
    agent = SUB_AGENTS[agent_key]
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=agent["system_prompt"],
        messages=[{"role": "user", "content": query}],
    )
    return "".join(block.text for block in resp.content if block.type == "text")


def _count_web_searches(content) -> int:
    """Count server-executed web_search calls in a response's content blocks."""
    return sum(
        1 for b in content
        if getattr(b, "type", "") == "server_tool_use" and getattr(b, "name", "") == "web_search"
    )


def run_web_search(query: str) -> tuple:
    """Run one live web search via Anthropic's server-side search tool.
    Returns (answer_text, number_of_searches_actually_performed)."""
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        tools=[WEB_SEARCH_TOOL],
        messages=[{"role": "user", "content": f"Search the web and answer concisely: {query}"}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    return text, _count_web_searches(resp.content)


def run_main_brain(user_message: str, history: List[Dict[str, str]],
                   system_prompt: str = None,
                   tools=DELEGATION_TOOLS, enable_search: bool = False,
                   persona: str = "owner") -> ChatResponse:
    timings: Dict[str, float] = {"precheck": 0.0, "model": 0.0, "tools": 0.0}
    _t0 = time.perf_counter()
    # Hard spend circuit breaker: checked BEFORE any Anthropic call is made,
    # so a capped persona costs nothing to refuse.
    cap = get_public_chat_cap() if persona == "public" else get_owner_chat_cap()
    capped_reply = PUBLIC_CAPPED_REPLY if persona == "public" else OWNER_CAPPED_REPLY
    if crm.get_chat_count(persona) >= cap:
        return ChatResponse(reply=capped_reply, delegated_to=[])
    # Fire-and-forget: the count write shouldn't block the reply. The cap
    # check above reads the cached snapshot, and set_setting updates that
    # cache in place, so this instance still counts accurately.
    threading.Thread(target=crm.increment_chat_count, args=(persona,), daemon=True).start()

    if system_prompt is None:
        system_prompt = build_main_brain_prompt(crm.get_setting(AGENT_NAME_KEY) or None)
    messages = list(history) + [{"role": "user", "content": user_message}]
    delegated_to: List[str] = []

    # Owner-only, metered search. Never enabled for the public widget -- that
    # caller simply never passes enable_search=True.
    search_available = False
    effective_tools = list(tools)
    effective_system_prompt = system_prompt
    if enable_search:
        if crm.get_search_count() < get_search_cap():
            search_available = True
            effective_tools = effective_tools + [WEB_SEARCH_TOOL]
        else:
            effective_system_prompt = system_prompt + SEARCH_CAPPED_NOTE
    timings["precheck"] = round(time.perf_counter() - _t0, 3)

    # Loop to allow multiple rounds of tool use (e.g. two sub-agents needed).
    for _ in range(4):
        _tm = time.perf_counter()
        resp = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=effective_system_prompt,
            tools=effective_tools,
            messages=messages,
        )
        timings["model"] = round(timings["model"] + (time.perf_counter() - _tm), 3)

        n_searches = _count_web_searches(resp.content)
        if n_searches:
            crm.increment_search_count(n_searches)
            delegated_to.append("Web Search")

        if resp.stop_reason != "tool_use":
            final_text = "".join(b.text for b in resp.content if b.type == "text")
            timings["total"] = round(time.perf_counter() - _t0, 3)
            return ChatResponse(reply=final_text, delegated_to=delegated_to, timings=timings)

        # Assistant turn included tool_use block(s); append it, then run each
        # tool and append the results, then loop back to let the Main Brain
        # compose its final answer.
        messages.append({"role": "assistant", "content": resp.content})
        _tt = time.perf_counter()
        tool_results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            agent_key = TOOL_NAME_TO_AGENT_KEY.get(block.name)
            if block.name == "set_agent_name":
                new_name = (block.input.get("name") or "").strip()
                ok = crm.set_setting(AGENT_NAME_KEY, new_name) if new_name else False
                delegated_to.append("Settings")
                answer = f"Saved -- you're now called {new_name}." if ok else "I heard the name but couldn't save it (settings store not connected)."
            elif block.name == "log_lead":
                delegated_to.append("CRM")
                answer = crm.create_lead(**block.input)
            elif block.name == "find_leads":
                delegated_to.append("CRM")
                answer = crm.list_leads(**block.input)
            elif block.name == "log_build_request":
                delegated_to.append("Build Queue")
                answer = crm.create_build_request(**block.input)
            elif block.name == "draft_email":
                delegated_to.append("Email (draft)")
                to = block.input.get("to", "")
                subject = block.input.get("subject", "")
                body_text = block.input.get("body", "")
                answer = f"DRAFT -- To: {to} | Subject: {subject}\n\n{body_text}"
            elif block.name == "send_email":
                delegated_to.append("Email (sent)")
                answer = emailer.send_email(
                    block.input.get("to", ""),
                    block.input.get("subject", ""),
                    block.input.get("body", ""),
                )
            elif block.name == "draft_social_post":
                delegated_to.append("Social (draft)")
                answer = social.create_draft(
                    block.input.get("platform", ""),
                    block.input.get("content", ""),
                    block.input.get("title", ""),
                    block.input.get("hashtags", ""),
                    block.input.get("media_url", ""),
                )
            elif block.name == "list_social_posts":
                delegated_to.append("Social Queue")
                answer = social.list_posts(block.input.get("status", ""))
            elif block.name == "publish_social_post":
                delegated_to.append("Social (published)")
                answer = social.publish_post(block.input.get("post_id", ""))
            elif block.name == "save_asset":
                delegated_to.append("Asset Library")
                answer = assets.add_asset(
                    block.input.get("name", ""),
                    block.input.get("url", ""),
                    block.input.get("media_type", "Photo"),
                    block.input.get("tags", ""),
                    block.input.get("notes", ""),
                )
            elif block.name == "find_assets":
                delegated_to.append("Asset Library")
                answer = assets.find_assets(
                    block.input.get("query", ""),
                    block.input.get("media_type", ""),
                )
            elif block.name == "log_event":
                delegated_to.append("Events Tracker")
                answer = events.add_event(**block.input)
            elif block.name == "find_events":
                delegated_to.append("Events Tracker")
                answer = events.list_events(**block.input)
            elif agent_key is None:
                answer = f"Unknown tool: {block.name}"
            else:
                delegated_to.append(SUB_AGENTS[agent_key]["name"])
                query = block.input.get("query", user_message)
                answer = call_sub_agent(agent_key, query)
                if answer.strip().startswith("NEEDS_SEARCH:"):
                    search_query = answer.split("NEEDS_SEARCH:", 1)[1].strip()
                    if search_available and crm.get_search_count() < get_search_cap():
                        search_text, used = run_web_search(search_query)
                        if used:
                            crm.increment_search_count(used)
                            delegated_to.append("Web Search")
                        answer = call_sub_agent(
                            agent_key,
                            f"{query}\n\nHere is current web search information you can use:\n{search_text}",
                        )
                    else:
                        answer = (
                            "I don't have live search access for that right now (the search "
                            "budget is capped for this period) -- Vinny will need to raise "
                            "the cap or check back next cycle."
                        )
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": answer,
                }
            )
        messages.append({"role": "user", "content": tool_results})
        timings["tools"] = round(timings["tools"] + (time.perf_counter() - _tt), 3)

    timings["total"] = round(time.perf_counter() - _t0, 3)
    return ChatResponse(
        reply="Sorry, I got stuck coordinating that -- try rephrasing your question.",
        delegated_to=delegated_to,
        timings=timings,
    )


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    result = run_main_brain(req.message, req.history, enable_search=True, persona="owner")
    # Persist the exchange to durable memory (Airtable) in the background --
    # the user shouldn't wait on bookkeeping after the reply is ready.
    _ts = time.perf_counter()

    def _persist(user_msg: str, reply: str) -> None:
        crm.save_turn("user", user_msg)
        crm.save_turn("assistant", reply)

    threading.Thread(target=_persist, args=(req.message, result.reply), daemon=True).start()
    result.timings["save"] = round(time.perf_counter() - _ts, 3)
    return result


@app.get("/api/history")
def history(limit: int = 40) -> JSONResponse:
    """Return recent conversation turns from durable memory (oldest first)."""
    return JSONResponse({"history": crm.get_history(limit)})


@app.get("/api/events")
def get_events(business: str = "") -> JSONResponse:
    """Owner-only. List events, optionally filtered to one side of the business."""
    return JSONResponse({"events": events.list_events_raw(business)})


TTS_VOICE_OPTIONS = [
    "en-GB-RyanNeural", "en-US-AndrewNeural", "en-US-GuyNeural",
    "en-US-EmmaNeural", "en-US-AriaNeural", "en-GB-SoniaNeural",
]


class SettingsUpdate(BaseModel):
    gmail_address: str = ""
    gmail_app_password: str = ""
    search_cap: str = ""
    public_chat_cap: str = ""
    owner_chat_cap: str = ""
    access_code: str = ""
    tts_voice: str = ""
    zapier_webhook_url: str = ""
    # Per-platform webhook overrides (each platform can have its own Zap; the
    # generic zapier_webhook_url above stays the Facebook fallback). Blank = untouched.
    zapier_webhook_url_facebook: str = ""
    zapier_webhook_url_instagram: str = ""
    zapier_webhook_url_youtube: str = ""
    zapier_webhook_url_tiktok: str = ""
    zapier_webhook_url_x: str = ""


@app.get("/api/settings")
def get_settings() -> JSONResponse:
    """Owner-only (behind the access gate). Reports connection status and
    current admin-editable config -- never echoes secrets (app password,
    access code) back to the frontend, only whether they're set/custom."""
    return JSONResponse({
        "gmail_address": emailer.get_gmail_address(),
        "gmail_connected": emailer.is_configured(),
        "search_cap": get_search_cap(),
        "search_count": crm.get_search_count(),
        "public_chat_cap": get_public_chat_cap(),
        "public_chat_count": crm.get_chat_count("public"),
        "owner_chat_cap": get_owner_chat_cap(),
        "owner_chat_count": crm.get_chat_count("owner"),
        "tts_voice": get_tts_voice(),
        "tts_voice_options": TTS_VOICE_OPTIONS,
        "access_code_is_custom": bool(crm.get_setting("access_code_override", "")),
        "social_connected": social.is_configured(),
        "social_platforms": social.connected_platforms(),
    })


@app.post("/api/settings")
def save_settings(req: SettingsUpdate) -> JSONResponse:
    """Owner-only. Saves connector credentials and admin config to Airtable
    Settings so they take effect immediately -- no Render redeploy needed.
    Blank fields are left untouched (so re-saving one field doesn't wipe
    another, and re-saving the address doesn't clear a saved password)."""
    if req.gmail_address.strip():
        crm.set_setting(emailer.GMAIL_ADDRESS_KEY, req.gmail_address.strip())
    if req.gmail_app_password.strip():
        crm.set_setting(emailer.GMAIL_APP_PASSWORD_KEY, req.gmail_app_password.strip())
    for key, field in (
        ("cap_search_monthly", req.search_cap),
        ("cap_chat_public", req.public_chat_cap),
        ("cap_chat_owner", req.owner_chat_cap),
    ):
        if field.strip():
            try:
                crm.set_setting(key, str(int(field.strip())))
            except ValueError:
                pass
    if req.tts_voice.strip():
        crm.set_setting("tts_voice_override", req.tts_voice.strip())
    if req.access_code.strip():
        crm.set_setting("access_code_override", req.access_code.strip())
    if req.zapier_webhook_url.strip():
        crm.set_setting(social.WEBHOOK_KEY, req.zapier_webhook_url.strip())
    for plat in ("facebook", "instagram", "youtube", "tiktok", "x"):
        val = getattr(req, f"zapier_webhook_url_{plat}", "").strip()
        if val:
            crm.set_setting(f"{social.WEBHOOK_KEY}_{plat}", val)
    return JSONResponse({
        "ok": True,
        "gmail_connected": emailer.is_configured(),
        "social_connected": social.is_configured(),
        "social_platforms": social.connected_platforms(),
    })


@app.get("/api/agent-name")
def agent_name() -> JSONResponse:
    """Return the name Susan has chosen for the assistant, if any."""
    return JSONResponse({"name": crm.get_setting(AGENT_NAME_KEY) or None})


@app.post("/api/public-chat", response_model=ChatResponse)
def public_chat(req: ChatRequest) -> ChatResponse:
    """Customer-facing chat for the embeddable website widget. Not gated.
    Uses the public persona + limited tools (answer + capture leads)."""
    name = crm.get_setting(AGENT_NAME_KEY) or "the assistant"
    return run_main_brain(req.message, req.history, build_public_prompt(name), PUBLIC_TOOLS, persona="public")


@app.get("/widget")
def widget() -> FileResponse:
    """Serve the public, embeddable chat widget (for the Wix site)."""
    return FileResponse("static/widget.html")


@app.get("/privacy")
def privacy() -> FileResponse:
    """Public privacy notice, linked from the widget footer."""
    return FileResponse("static/privacy.html")


class TTSRequest(BaseModel):
    text: str


async def _grok_tts(text: str) -> bytes:
    """Grok (xAI) neural TTS -> MP3 bytes. Raises on any failure, surfacing
    the API's own error body so we can see exactly what it wants."""
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.post(
            "https://api.x.ai/v1/tts",
            headers={
                "Authorization": f"Bearer {XAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"text": text, "voice_id": XAI_VOICE, "language": "en"},
        )
    if r.status_code != 200:
        raise RuntimeError(f"xAI {r.status_code}: {r.text[:600]}")
    if not r.content:
        raise ValueError("empty audio")
    return r.content


async def _edge_tts(text: str) -> bytes:
    """Free Microsoft Edge neural TTS -> MP3 bytes."""
    communicate = edge_tts.Communicate(text, get_tts_voice(), rate=TTS_RATE)
    audio = bytearray()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio.extend(chunk["data"])
    return bytes(audio)


@app.post("/api/tts")
async def tts(req: TTSRequest) -> Response:
    """Return spoken MP3 for the text. Uses Grok TTS if XAI_API_KEY is set,
    otherwise the free Edge TTS. Falls back to Edge if Grok errors."""
    text = req.text.strip()[:3000]
    if not text:
        return Response(status_code=400)
    if XAI_API_KEY:
        try:
            data = await _grok_tts(text)
            return Response(data, media_type="audio/mpeg", headers={"X-TTS-Engine": "grok"})
        except Exception as e:
            # Fall back to the free voice rather than going silent, but log why.
            print(f"[tts] Grok TTS failed, using Edge fallback: {type(e).__name__}: {e}", flush=True)
            data = await _edge_tts(text)
            return Response(data, media_type="audio/mpeg", headers={"X-TTS-Engine": "edge-fallback"})
    return Response(await _edge_tts(text), media_type="audio/mpeg", headers={"X-TTS-Engine": "edge"})


# User management endpoints (owner only, behind access gate)


class AddUserRequest(BaseModel):
    username: str
    email: str
    password: str
    role: str = "owner"


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@app.get("/api/users")
def get_users() -> JSONResponse:
    """Owner-only. List all users."""
    return JSONResponse({"ok": True, "users": users.list_users()})


@app.post("/api/users")
def add_user(req: AddUserRequest) -> JSONResponse:
    """Owner-only. Create a new user account."""
    if not req.username or not req.email or not req.password:
        return JSONResponse({"ok": False, "detail": "Missing required fields"}, status_code=400)
    if users.add_user(req.username, req.email, req.password, req.role):
        return JSONResponse({"ok": True, "detail": "User created"})
    return JSONResponse({"ok": False, "detail": "User already exists or error creating user"}, status_code=400)


@app.delete("/api/users/{username}")
def delete_user(username: str) -> JSONResponse:
    """Owner-only. Delete a user account."""
    if users.delete_user(username):
        return JSONResponse({"ok": True, "detail": "User deleted"})
    return JSONResponse({"ok": False, "detail": "User not found"}, status_code=400)


@app.post("/api/change-password")
def change_password(req: ChangePasswordRequest, request: Request) -> JSONResponse:
    """Any logged-in user. Change their own password."""
    session_token = request.cookies.get("cc_session")
    if not session_token:
        return JSONResponse({"ok": False, "detail": "Not logged in"}, status_code=401)
    username = users.verify_session_token(session_token)
    if not username:
        return JSONResponse({"ok": False, "detail": "Invalid session"}, status_code=401)
    user = users.get_user(username)
    if not user or not users.verify_password(req.current_password, user["password_hash"]):
        return JSONResponse({"ok": False, "detail": "Current password is incorrect"}, status_code=401)
    if not users.update_user_password(username, req.new_password):
        return JSONResponse({"ok": False, "detail": "Failed to update password"}, status_code=500)
    return JSONResponse({"ok": True, "detail": "Password updated"})


# Serve the frontend
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    return FileResponse("static/index.html")
