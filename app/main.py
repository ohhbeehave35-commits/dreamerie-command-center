"""
FastAPI backend for The Dreamerie / Suzy D command center (Susan's business).
Built from the Stinger Industries delivery playbook.

Run with:
    uvicorn app.main:app --reload --port 8000

Requires ANTHROPIC_API_KEY set in the environment (see .env.example).
"""

import os
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

app = FastAPI(title="The Dreamerie Command Center")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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
  if (r.ok) location.reload(); else document.getElementById('m').textContent = 'Incorrect code';
});
</script></body></html>"""


@app.middleware("http")
async def access_gate(request: Request, call_next):
    if not ACCESS_CODE:
        return await call_next(request)
    # Public, customer-facing paths (the website chat widget) are never gated.
    if request.url.path in ("/api/unlock", "/api/public-chat", "/widget"):
        return await call_next(request)
    if request.cookies.get("cc_access") == ACCESS_CODE:
        return await call_next(request)
    if request.url.path.startswith("/api/") or request.url.path.startswith("/static/"):
        return JSONResponse({"detail": "locked"}, status_code=401)
    return HTMLResponse(LOCK_PAGE, status_code=401)


class UnlockRequest(BaseModel):
    code: str


@app.post("/api/unlock")
def unlock(req: UnlockRequest) -> JSONResponse:
    if ACCESS_CODE and req.code == ACCESS_CODE:
        resp = JSONResponse({"ok": True})
        resp.set_cookie("cc_access", ACCESS_CODE, max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax")
        return resp
    return JSONResponse({"ok": False}, status_code=401)


class ChatRequest(BaseModel):
    message: str
    history: List[Dict[str, str]] = []  # [{"role": "user"|"assistant", "content": "..."}]


class ChatResponse(BaseModel):
    reply: str
    delegated_to: List[str] = []


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
                   tools=DELEGATION_TOOLS, enable_search: bool = False) -> ChatResponse:
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
        if crm.get_search_count() < SEARCH_MONTHLY_CAP:
            search_available = True
            effective_tools = effective_tools + [WEB_SEARCH_TOOL]
        else:
            effective_system_prompt = system_prompt + SEARCH_CAPPED_NOTE

    # Loop to allow multiple rounds of tool use (e.g. two sub-agents needed).
    for _ in range(4):
        resp = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=effective_system_prompt,
            tools=effective_tools,
            messages=messages,
        )

        n_searches = _count_web_searches(resp.content)
        if n_searches:
            crm.increment_search_count(n_searches)
            delegated_to.append("Web Search")

        if resp.stop_reason != "tool_use":
            final_text = "".join(b.text for b in resp.content if b.type == "text")
            return ChatResponse(reply=final_text, delegated_to=delegated_to)

        # Assistant turn included tool_use block(s); append it, then run each
        # tool and append the results, then loop back to let the Main Brain
        # compose its final answer.
        messages.append({"role": "assistant", "content": resp.content})
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
            elif agent_key is None:
                answer = f"Unknown tool: {block.name}"
            else:
                delegated_to.append(SUB_AGENTS[agent_key]["name"])
                query = block.input.get("query", user_message)
                answer = call_sub_agent(agent_key, query)
                if answer.strip().startswith("NEEDS_SEARCH:"):
                    search_query = answer.split("NEEDS_SEARCH:", 1)[1].strip()
                    if search_available and crm.get_search_count() < SEARCH_MONTHLY_CAP:
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

    return ChatResponse(
        reply="Sorry, I got stuck coordinating that -- try rephrasing your question.",
        delegated_to=delegated_to,
    )


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    result = run_main_brain(req.message, req.history, enable_search=True)
    # Persist the exchange to durable memory (Airtable) so questions and answers
    # are remembered forever, across devices and browser sessions.
    crm.save_turn("user", req.message)
    crm.save_turn("assistant", result.reply)
    return result


@app.get("/api/history")
def history(limit: int = 40) -> JSONResponse:
    """Return recent conversation turns from durable memory (oldest first)."""
    return JSONResponse({"history": crm.get_history(limit)})


@app.get("/api/agent-name")
def agent_name() -> JSONResponse:
    """Return the name Susan has chosen for the assistant, if any."""
    return JSONResponse({"name": crm.get_setting(AGENT_NAME_KEY) or None})


@app.post("/api/public-chat", response_model=ChatResponse)
def public_chat(req: ChatRequest) -> ChatResponse:
    """Customer-facing chat for the embeddable website widget. Not gated.
    Uses the public persona + limited tools (answer + capture leads)."""
    name = crm.get_setting(AGENT_NAME_KEY) or "the assistant"
    return run_main_brain(req.message, req.history, build_public_prompt(name), PUBLIC_TOOLS)


@app.get("/widget")
def widget() -> FileResponse:
    """Serve the public, embeddable chat widget (for the Wix site)."""
    return FileResponse("static/widget.html")


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
    communicate = edge_tts.Communicate(text, TTS_VOICE, rate=TTS_RATE)
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


# Serve the frontend
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    return FileResponse("static/index.html")
