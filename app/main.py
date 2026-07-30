"""
FastAPI backend for the Stinger Industries / Ohh Beehave command center.

Run with:
    uvicorn app.main:app --reload --port 8000

Requires ANTHROPIC_API_KEY set in the environment (see .env.example).
"""

import json
import logging
import os
import re
import secrets
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional

import edge_tts
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import (
    FileResponse, Response, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse,
    PlainTextResponse,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator
from anthropic import Anthropic
from openai import OpenAI

from . import crm
from . import inbox
from . import unbuilt
from . import events
from . import brand_identity
from . import password_reset
from . import toolbox
from . import emailer
from . import calendar as gcal
from . import social
from . import voice_eleven
from . import assets
from . import memory
from . import video_cost
from . import users
from . import results
from . import brand
from . import hubspot
from . import stripe_billing
from . import media_gen
from . import diagnostic
from . import files_dropbox
from . import files_gdrive
from . import push
from . import webfetch
from . import seo_audit
from . import config_check
from . import support
from . import passkeys
from . import signed_tokens
from .agents import (
    MAIN_BRAIN_SYSTEM_PROMPT,
    build_main_brain_prompt,
    build_public_prompt,
    get_automation_level_prompt,
    SUB_AGENTS,
    DELEGATION_TOOLS,
    TOOL_NAME_TO_AGENT_KEY,
    MODE_TOOLS,
    MODE_PROMPTS,
    PUBLIC_SYSTEM_PROMPT,
    PUBLIC_TOOLS,
)

AGENT_NAME_KEY = "agent_name"

def _current_agent_name():
    """The name Susan gave the assistant, or None while unnamed."""
    try:
        return crm.get_setting(AGENT_NAME_KEY) or None
    except Exception:
        return None

log = logging.getLogger(__name__)

# Make sure INFO-level logs actually reach Render's log stream. Uvicorn sets
# up its own handlers but not on our app's namespace; without this, log.info
# calls silently vanish. Also give lines a timestamp Render can grep.
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
else:
    logging.getLogger().setLevel(logging.INFO)

load_dotenv(override=True)  # .env wins over any stale system-level key

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
# Neural voice for spoken replies (free via Edge TTS). Try e.g. en-US-AndrewNeural,
# en-US-EmmaNeural, en-GB-SoniaNeural. Override with TTS_VOICE in .env.
TTS_VOICE = os.environ.get("TTS_VOICE", "en-GB-RyanNeural")
# Speaking pace. Sonia/Ryan are already unhurried newsreader voices, so the old
# "-6%" default stacked slow on slow and read as lethargic. A slight push above
# baseline lands closer to how a person actually talks. Tune via env.
TTS_RATE = os.environ.get("TTS_RATE", "+4%")
# Grok (xAI) TTS: set XAI_API_KEY to use it; otherwise free Edge TTS is used.
# Same endpoint as originally wired (POST /v1/tts) -- xAI didn't ship a newer
# API, they expanded the voice roster: the original 5 (ara, eve, leo, rex,
# sal) plus 21 new flagship voices added July 2026 (Carina, Zagan, Helix,
# Orion, Luna, Iris, Altair, Zenith, Perseus, Helios, Lux, Kepler, Rigel,
# Cosmo, Celeste, Ursa, Sirius, Lumen, Castor, Naksh, Atlas), all natively
# multilingual (25+ languages). Carina is documented as tuned for "soft,
# empathetic customer service tones" -- worth trying for Annabelle's public
# persona; any of the 26 IDs work as-is with the existing integration, or a
# cloned voice_id.
XAI_API_KEY = os.environ.get("XAI_API_KEY", "")
XAI_VOICE = os.environ.get("XAI_VOICE", "eve")
client = Anthropic()  # reads ANTHROPIC_API_KEY from env

# OpenAI integration (ChatGPT). Key is stored in Airtable settings.
OPENAI_API_KEY_SETTING = "openai_api_key"

def _get_openai_key() -> str:
    """Get OpenAI API key from Airtable settings, fallback to env var."""
    if crm.is_configured():
        key = crm.get_setting(OPENAI_API_KEY_SETTING, "")
        if key:
            return key
    return os.environ.get("OPENAI_API_KEY", "")

def is_openai_configured() -> bool:
    """Check if OpenAI API key is configured."""
    return bool(_get_openai_key())

def get_openai_client() -> Optional[OpenAI]:
    """Get OpenAI client if configured, else None."""
    key = _get_openai_key()
    return OpenAI(api_key=key) if key else None

# Owner-only, metered live web search. Never added to PUBLIC_TOOLS -- the
# customer-facing widget on the Ohh Beehave site can never trigger a search.
# The cap resets monthly (see crm._search_usage_key); once hit, Annabelle is
# told to say so plainly rather than silently going quiet.
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search"}
SEARCH_MONTHLY_CAP = int(os.environ.get("SEARCH_MONTHLY_CAP", "50"))
SEARCH_CAPPED_NOTE = (
    "\n\nNOTE: the web search budget for this billing period has been reached. "
    "If asked to search, do not attempt it -- tell Vinny plainly that search is "
    "capped for now and he needs to raise SEARCH_MONTHLY_CAP or wait for next "
    "month's reset."
)

# Platform-wide spend guardrail: a hard monthly cap on ordinary chat turns,
# checked BEFORE any Anthropic call is made (a true circuit breaker, not just
# a polite refusal after spending money). Public gets a much lower default
# than owner, since it's the one surface a stranger or a bot can hit freely.
PUBLIC_MONTHLY_CAP = int(os.environ.get("PUBLIC_MONTHLY_CAP", "300"))
OWNER_MONTHLY_CAP = int(os.environ.get("OWNER_MONTHLY_CAP", "2000"))
PUBLIC_CAPPED_REPLY = (
    "Thanks so much for reaching out! Our assistant is temporarily at capacity "
    "for the moment -- please call or email us directly and we'll get right "
    "back to you, or feel free to try again a bit later."
)
OWNER_CAPPED_REPLY = (
    "I've hit my chat budget for this billing period, Vinny -- you'll need to "
    "raise OWNER_MONTHLY_CAP in the environment settings or wait for next "
    "month's reset."
)

# AI image/video generation (xAI Grok Imagine, app/media_gen.py) is available
# to the PUBLIC widget (see agents.PUBLIC_TOOLS), so these caps are a real
# spend guardrail on a surface a stranger can hit freely -- checked before
# calling xAI, same circuit-breaker discipline as the search/chat caps above.
# Video costs meaningfully more per call than an image, hence the lower default.
IMAGE_MONTHLY_CAP = int(os.environ.get("IMAGE_MONTHLY_CAP", "100"))
VIDEO_MONTHLY_CAP = int(os.environ.get("VIDEO_MONTHLY_CAP", "15"))
IMAGE_CAPPED_REPLY = (
    "The image-generation budget for this billing period has been reached. "
    "Raise IMAGE_MONTHLY_CAP (or the Settings override) or wait for next month's reset."
)
VIDEO_CAPPED_REPLY = (
    "The video-generation budget for this billing period has been reached. "
    "Raise VIDEO_MONTHLY_CAP (or the Settings override) or wait for next month's reset."
)


def _int_override(key: str, default: int) -> int:
    """Read an admin-editable int setting from Airtable, falling back to the
    env-var default if unset, invalid, or non-positive."""
    raw = crm.get_setting(key, "")
    if not raw:
        return default
    return crm._parse_cap(raw, default)


# Admin-editable overrides (Settings panel) win over env vars, take effect
# immediately -- no redeploy. Functions, not constants, so every request
# sees the latest value.
def get_search_cap() -> int:
    return _int_override("cap_search_monthly", SEARCH_MONTHLY_CAP)


def get_public_chat_cap() -> int:
    return _int_override("cap_chat_public", PUBLIC_MONTHLY_CAP)


def get_owner_chat_cap() -> int:
    return _int_override("cap_chat_owner", OWNER_MONTHLY_CAP)


def get_image_cap() -> int:
    return _int_override("cap_image_monthly", IMAGE_MONTHLY_CAP)


def get_video_cap() -> int:
    return _int_override("cap_video_monthly", VIDEO_MONTHLY_CAP)


def get_tts_voice() -> str:
    return crm.get_setting("tts_voice_override", "") or TTS_VOICE


AUTOMATION_LEVELS = ("manual", "semi_auto", "full_auto")


def get_automation_level() -> str:
    return crm.get_setting("automation_level", "") or "manual"


def get_user_tts_voice(username: Optional[str] = None) -> str:
    """TTS voice for a specific user; falls back to global setting."""
    if username:
        v = crm.get_user_setting(username, "tts_voice_override", "")
        if v:
            return v
    return get_tts_voice()


def get_user_model_key(username: Optional[str] = None) -> str:
    """Chat model slug for a specific user; falls back to account default."""
    if username:
        m = crm.get_user_setting(username, "chat_model_default", "")
        if m in MODEL_CHOICES:
            return m
    return get_default_model_key()


def get_user_automation_level(username: Optional[str] = None) -> str:
    """Automation level for a specific user; falls back to account default."""
    if username:
        a = crm.get_user_setting(username, "automation_level", "")
        if a in AUTOMATION_LEVELS:
            return a
    return get_automation_level()


# ── AI MODEL SELECTION ───────────────────────────────────────────────────────
# Which Claude model powers Annabelle. Picked per-message from the chat bar, or
# left on "auto" to use the account default (Settings -> AI Model). Keyed by a
# short slug rather than the raw model id so the frontend never sends an
# arbitrary string straight into the API -- anything not in this table falls
# back to the default, which is the whole point of the allowlist.
MODEL_CHOICES = {
    "opus-5": {
        "id": "claude-opus-5",
        "label": "Opus 5",
        "blurb": "Deepest reasoning. Best for proposals, audits, and hard problems. Slowest and priciest.",
    },
    "sonnet-5": {
        "id": "claude-sonnet-5",
        "label": "Sonnet 5",
        "blurb": "Balanced speed and smarts. The everyday default.",
    },
    "haiku-4-5": {
        "id": "claude-haiku-4-5",
        "label": "Haiku 4.5",
        "blurb": "Fastest and cheapest. Good for quick lookups and short answers.",
    },
}
DEFAULT_MODEL_KEY = "sonnet-5"


def get_default_model_key() -> str:
    """The account-wide default model slug (Settings -> AI Model)."""
    saved = crm.get_setting("chat_model_default", "")
    if saved in MODEL_CHOICES:
        return saved
    # Honor a CLAUDE_MODEL env override if it names one of the choices, so an
    # existing deployment's env var keeps working without a Settings save.
    for key, spec in MODEL_CHOICES.items():
        if spec["id"] == MODEL:
            return key
    return DEFAULT_MODEL_KEY


def resolve_model(key: str = "") -> str:
    """Turn a slug from the chat bar into a real model id.

    Blank / "auto" / anything unrecognized resolves to the account default, so
    a stale or hand-edited client can never send us an unsupported model id.
    """
    key = (key or "").strip().lower()
    if key in MODEL_CHOICES:
        return MODEL_CHOICES[key]["id"]
    return MODEL_CHOICES.get(get_default_model_key(), MODEL_CHOICES[DEFAULT_MODEL_KEY])["id"]


def get_access_code() -> str:
    return crm.get_setting("access_code_override", "") or ACCESS_CODE


app = FastAPI(title="The Dreamerie Command Center")

# Canonical public origin. Used by robots.txt, sitemap.xml and the canonical/OG
# tags on the marketing pages. NOTE (27 Jul 2026): stingerindustries.ai does not
# resolve and stingerindustries.com redirects to an unrelated company, so this
# Render hostname is currently the only working public origin. Point this at the
# real domain the moment one is registered -- canonical tags and the sitemap
# follow it automatically.
_SITE_BASE = os.environ.get("SITE_BASE_URL", "https://dreamerie-command-center.onrender.com").rstrip("/")

ALLOWED_ORIGINS = [
    "https://dreamerie-command-center.onrender.com",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    return response


@app.on_event("startup")
def startup_init():
    """Startup checks. This used to CREATE a default owner/changeme account --
    a published credential on an access-code deployment that never needed it
    (Susan signs in with the code; nothing in her UI even calls /api/login).
    Creation is gone. If the account already exists on Airtable it is flagged
    on the /support page every boot until it is deleted from Settings → Users,
    and login-time defense lives in the /api/login handler."""
    if not crm.is_configured():
        return  # No Airtable, skip
    try:
        existing = {u.get("username") for u in users.list_users()}
    except Exception:
        return  # Airtable blip at boot; the login-time guard still holds
    if "owner" in existing:
        support.record_note(
            "default_credential",
            "The 'owner' default account still exists on Airtable. If its "
            "password is still 'changeme' it is a PUBLISHED credential -- "
            "delete the account in Settings → Users (or reset its password).")


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
<div id="codeBox" style="display:flex;flex-direction:column;gap:14px;align-items:center">
<input id="c" type="password" placeholder="Access code" autocomplete="current-password" style="margin-top:10px;padding:11px 14px;font-size:16px;width:220px;color:#e6e2d6;background:rgba(26,18,34,0.9);border:1px solid rgba(200,204,210,0.2);border-radius:10px;text-align:center">
<button id="unlockBtn" style="padding:10px 26px;font-weight:600;font-size:14px;background:linear-gradient(180deg,#d9a8ec,#b87ad9);color:#241530;border:none;border-radius:10px;cursor:pointer">Unlock</button>
<button id="pk" type="button" style="display:none;padding:9px 22px;font-weight:600;font-size:13px;background:none;color:#d9a8ec;border:1px solid rgba(196,150,230,0.5);border-radius:10px;cursor:pointer">Use fingerprint / Face ID</button>
</div>
<div id="userBox" style="display:none;flex-direction:column;gap:14px;align-items:center">
<input id="u" type="text" placeholder="Username" autocomplete="username" style="margin-top:10px;padding:11px 14px;font-size:16px;width:220px;color:#e6e2d6;background:rgba(26,18,34,0.9);border:1px solid rgba(200,204,210,0.2);border-radius:10px;text-align:center">
<input id="p" type="password" placeholder="Password" autocomplete="current-password" style="padding:11px 14px;font-size:16px;width:220px;color:#e6e2d6;background:rgba(26,18,34,0.9);border:1px solid rgba(200,204,210,0.2);border-radius:10px;text-align:center">
<label style="display:flex;gap:8px;align-items:center;font-size:12px;color:#c8ccd2;cursor:pointer"><input id="rm" type="checkbox" style="accent-color:#b87ad9"> Stay signed in on this device</label>
<button id="loginBtn" type="button" style="padding:10px 26px;font-weight:600;font-size:14px;background:linear-gradient(180deg,#d9a8ec,#b87ad9);color:#241530;border:none;border-radius:10px;cursor:pointer">Sign in</button>
</div>
<a id="toggleMode" href="#" style="font-size:12px;color:#9aa0aa;text-decoration:none;border-bottom:1px dotted rgba(200,204,210,0.35)">Have your own username? Sign in here</a>
<div id="m" style="font-size:12px;color:#e0a48f;min-height:16px"></div>
</form>
<script>
// Two doors, one gate. The access code stays the default and is what Susan
// sees first; the username form is opt-in for people with their own account
// (Nick). Submitting the form runs whichever door is currently showing --
// the Enter key must never silently fire the wrong one.
const m = document.getElementById('m');
let userMode = false;
document.getElementById('toggleMode').addEventListener('click', (e) => {
  e.preventDefault();
  userMode = !userMode;
  document.getElementById('codeBox').style.display = userMode ? 'none' : 'flex';
  document.getElementById('userBox').style.display = userMode ? 'flex' : 'none';
  e.target.textContent = userMode ? 'Use the shared access code instead' : 'Have your own username? Sign in here';
  m.textContent = '';
  (userMode ? document.getElementById('u') : document.getElementById('c')).focus();
});

async function unlockWithCode() {
  const r = await fetch('/api/unlock', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code: document.getElementById('c').value }) });
  if (r.ok) { location.reload(); return; }
  const j = await r.json().catch(() => ({}));
  m.textContent = j.detail || 'Incorrect code';
}

async function signInWithUsername() {
  const u = document.getElementById('u').value.trim();
  const p = document.getElementById('p').value;
  if (!u || !p) { m.textContent = 'Enter your username and password.'; return; }
  m.textContent = 'Signing in...';
  const r = await fetch('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: u, password: p, remember_me: document.getElementById('rm').checked }) });
  if (r.ok) { location.reload(); return; }
  const j = await r.json().catch(() => ({}));
  m.textContent = j.detail || 'Invalid username or password';
}

document.getElementById('f').addEventListener('submit', (e) => {
  e.preventDefault();
  (userMode ? signInWithUsername : unlockWithCode)();
});
document.getElementById('loginBtn').addEventListener('click', signInWithUsername);
// Fingerprint / Face ID. Additive: the code input above is untouched and
// always works. Manual base64url conversion instead of the parse*FromJSON
// helpers -- those need Safari 17.4+/Chrome 118+ and we don't know her iOS.
const b64u = {
  dec: s => Uint8Array.from(atob(s.replace(/-/g,'+').replace(/_/g,'/')), c => c.charCodeAt(0)),
  enc: b => btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=+$/,'')
};
if (window.PublicKeyCredential) {
  fetch('/api/passkey/enabled').then(r => r.json()).then(d => {
    if (d.enabled) document.getElementById('pk').style.display = 'block';
  }).catch(() => {});
}
document.getElementById('pk').addEventListener('click', async () => {
  const m = document.getElementById('m');
  try {
    const o = await fetch('/api/passkey/auth-options', { method: 'POST' }).then(r => r.json());
    if (!o.options) { m.textContent = o.detail || 'Fingerprint sign-in unavailable -- use your access code.'; return; }
    const pub = o.options;
    pub.challenge = b64u.dec(pub.challenge);
    (pub.allowCredentials || []).forEach(c => c.id = b64u.dec(c.id));
    const cred = await navigator.credentials.get({ publicKey: pub });
    const body = { state: o.state, credential: { id: cred.id, rawId: b64u.enc(cred.rawId), type: cred.type,
      response: { clientDataJSON: b64u.enc(cred.response.clientDataJSON),
                  authenticatorData: b64u.enc(cred.response.authenticatorData),
                  signature: b64u.enc(cred.response.signature),
                  userHandle: cred.response.userHandle ? b64u.enc(cred.response.userHandle) : null } } };
    const r = await fetch('/api/passkey/auth', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (r.ok) { location.reload(); return; }
    m.textContent = (await r.json().catch(() => ({}))).detail || "Fingerprint didn't work -- your access code always does.";
  } catch (e) {
    m.textContent = 'Fingerprint cancelled -- you can always use your access code.';
  }
});
</script></body></html>"""


@app.middleware("http")
async def access_gate(request: Request, call_next):
    if not ACCESS_CODE and not crm.is_configured():
        return await call_next(request)
    # Public, customer-facing paths (the website chat widget) are never gated.
    if (request.url.path in (
            # /api/unlock MUST be here: it is the door in the gate. Gate it
            # and the Unlock button 401s before routing, which is exactly how
            # this deployment locked everyone out.
            "/api/unlock",
            # The fingerprint door in the same gate. Gating any of these three
            # reproduces the /api/unlock lockout verbatim: the lock page's
            # passkey button would 401 before routing. The register/credentials
            # endpoints are deliberately NOT here -- a stranger must never be
            # able to enroll a fingerprint.
            "/api/passkey/enabled", "/api/passkey/auth-options", "/api/passkey/auth",
            "/api/login", "/api/public-chat", "/api/brand", "/widget",
            "/privacy", "/auth/google-callback",
            "/auth/drive-callback", "/dropbox/callback",
            "/lightspeed/callback",
            "/api/webhooks/stripe",
            "/api/proposals/sign",  # proposal viewer accessed by clients, not owners
            "/proposal",  # client-facing proposal viewer
            "/healthz",  # uptime monitor probe -- must never require auth
            "/api/inbox",  # machine-to-machine push from Stinger -- protected by X-Inbox-Secret, not a session
            # The browser's error beacon. Ungated ON PURPOSE: the failure we
            # most needed to see -- "I can't get in" -- happens on a page that
            # by definition has no valid cookie, so a gated beacon would be
            # silent for exactly the incident it exists to report. It accepts
            # no data that can be read back without the gate, is rate-limited
            # per IP, and everything it stores is scrubbed and length-capped.
            "/api/support/client-event",
            "/run/calendar-reminder",  # GitHub Actions cron trigger -- protected by its own secret, not a session
                                                # Crawlers fetch these before anything else. Behind the gate they
            # returned 401, so the site had no sitemap and no robots policy.
            "/robots.txt",
            # PWA install assets. These must be reachable WITHOUT a session:
            # the browser fetches the manifest and registers the service worker
            # from the page context, and a 401 on either one silently makes the
            # app non-installable ("Add to Home Screen" never appears, and no
            # service worker means no push and no offline shell). They contain
            # nothing private -- app name, colors, and the logo that's already
            # public on the marketing site.
            "/static/manifest.json", "/static/sw.js",
            "/static/apple-touch-icon.png", "/static/favicon-32.png",
            # Marketing images referenced BY the public pages. Without these the
            # gate returned 401 for the logo in the nav of every public page and
            # for the two hero visuals on /ai-solutions -- so every prospect who
            # ever loaded the site saw broken images. The pages were public; the
            # images they point at were not. Nothing private here: this is the
            # same logo already printed on client documents.
            "/static/logo.webp", "/static/braincenter.webp", "/static/brainagent.webp",
        )
                                                or request.url.path.startswith("/static/icon-")  # PWA app icons
            or request.url.path.startswith("/artifact/")):  # client-facing document link
        return await call_next(request)
    # Check for valid session token in cookie. Also confirm the user still
    # exists -- fixes open finding #2 (deleted user's cookies remaining valid
    # until natural TTL). Cached (5 min TTL) so this doesn't add an Airtable
    # round-trip to every request; the tradeoff is up to 5 min of extra
    # access for a deleted user, vs the previous unbounded 30 days.
    session_token = request.cookies.get("cc_session")
    if session_token:
        username = users.verify_session_token(session_token)
        if username and users.user_exists_cached(username):
            return await call_next(request)
    # Gate cookie. Guarded on get_access_code(), NOT the raw ACCESS_CODE env
    # var: the code may have been set from the Settings panel
    # (access_code_override) with the env var never set, and keying this on
    # the env var meant a correct unlock still could not get past the gate.
    # The truthiness check also stops an empty cookie matching an empty code.
    _gate_code = get_access_code()
    if _gate_code and secrets.compare_digest(request.cookies.get("cc_access", ""), _gate_code):
        return await call_next(request)
    # A direct/bookmarked/emailed link to an app PAGE (basic.html, index.html,
    # ...) must land on the sign-in form, not raw JSON -- a human just
    # navigated here and expects something to read, not fetch() data. Only
    # non-page static assets (and /api/) get the JSON the frontend's own
    # fetch() calls know how to handle.
    if request.url.path.startswith("/static/") and request.url.path.endswith(".html"):
        return HTMLResponse(LOCK_PAGE, status_code=401)
    if request.url.path.startswith("/api/") or request.url.path.startswith("/static/"):
        return JSONResponse({"detail": "locked"}, status_code=401)
    return HTMLResponse(LOCK_PAGE, status_code=401)


# Paths a customer can reach. They get a warm, vague message -- a stranger on
# the website should never learn that the account is behind on a bill.
_PUBLIC_PATHS = ("/api/public-chat", "/widget", "/artifact/", "/proposal")


def _explain_upstream_failure(exc: Exception, path: str) -> tuple:
    """Turn an unhandled exception into an honest, actionable message.

    Written after a live 500 that read "internal error" and sent us looking for
    a downed server. The server was fine -- the Anthropic account was out of
    credit. The class of failure that costs the most time is the one that
    describes itself wrongly.
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    is_public = any(path.startswith(p) for p in _PUBLIC_PATHS)

    if "credit balance is too low" in text or "billing" in text:
        if is_public:
            return ("Sorry -- the assistant is briefly unavailable. "
                    "Please leave your details and we'll get right back to you.", 503)
        return ("The Anthropic account is out of credit, so the assistant can't "
                "reply. The server is fine. Top up at console.anthropic.com "
                "(Plans & Billing) and it resumes immediately.", 503)

    if "authentication_error" in text or "invalid x-api-key" in text or "invalid api key" in text:
        if is_public:
            return ("Sorry -- the assistant is briefly unavailable. "
                    "Please leave your details and we'll get right back to you.", 503)
        return ("The Anthropic API key is missing or invalid. Check "
                "ANTHROPIC_API_KEY in the Render dashboard.", 503)

    if "rate_limit" in text or "overloaded" in text or "529" in text:
        return ("The AI service is rate-limited or overloaded right now. "
                "Wait a moment and try again.", 503)

    return ("internal error", 500)


@app.middleware("http")
async def request_logger(request: Request, call_next):
    """Log every request with method, path, status, and duration. On any
    unhandled exception, log the full traceback and return 500 (so the worker
    stays alive -- previously an exception here would crash the worker and
    Cloudflare would surface a 520)."""
    start = time.perf_counter()
    path = request.url.path
    method = request.method
    try:
        response = await call_next(request)
    except Exception as exc:
        dur_ms = int((time.perf_counter() - start) * 1000)
        log.error(
            "REQ_FAIL %s %s -> 500 in %dms\n%s",
            method, path, dur_ms, traceback.format_exc(),
        )
        # Also keep it where someone can actually SEE it. The Render log stream
        # is only readable by whoever is logged into Render and watching at the
        # time; this puts the same failure on the support page.
        support.record_request(method, path, 500, dur_ms,
                               error=f"{type(exc).__name__}: {exc}")
        # An out-of-credit or bad-key Anthropic account is not an "internal
        # error" -- it is a two-minute fix, and reporting it as a generic 500
        # sends you hunting for a server problem that doesn't exist. Ask the
        # exception what happened and say so.
        detail, status = _explain_upstream_failure(exc, path)
        return JSONResponse({"detail": detail}, status_code=status)
    dur_ms = int((time.perf_counter() - start) * 1000)
    # Skip noisy healthz / static asset requests unless slow or errored
    is_noisy = path == "/healthz" or path.startswith("/static/")
    if response.status_code >= 500 or (not is_noisy) or dur_ms > 2000:
        level = logging.WARNING if response.status_code >= 400 else logging.INFO
        log.log(level, "REQ %s %s -> %d in %dms", method, path, response.status_code, dur_ms)
    # record_request keeps only failures and unusually slow calls; a successful
    # fast request is dropped on the floor there, not filtered here, so the rule
    # lives in one place.
    support.record_request(method, path, response.status_code, dur_ms)
    return response


class LoginRequest(BaseModel):
    username: str
    password: str
    remember_me: bool = False


# Brute-force protection on the access gate: per-IP sliding-window lockout.
# In-memory (resets on redeploy/restart) -- fine for a single-instance app;
# the goal is defeating a simple automated guesser, not surviving a restart.
UNLOCK_MAX_ATTEMPTS = int(os.environ.get("UNLOCK_MAX_ATTEMPTS", "5"))
UNLOCK_WINDOW_SECONDS = int(os.environ.get("UNLOCK_WINDOW_SECONDS", "900"))  # 15 min
_unlock_attempts: Dict[str, list] = {}

# Global backstop: even if an attacker forges X-Forwarded-For to make every
# guess look like a new IP, cap the TOTAL failed logins across all IPs in the
# window. Set well above what a few real users fat-fingering passwords would hit,
# but far below what a credential-guessing run needs. Tunable via env.
UNLOCK_GLOBAL_MAX_ATTEMPTS = int(os.environ.get("UNLOCK_GLOBAL_MAX_ATTEMPTS", "50"))
_global_unlock_attempts: list = []

# How many trusted reverse-proxy hops sit in front of the app. On Render this is
# 1 (Render's load balancer). If you put Cloudflare in front too, set it to 2.
TRUSTED_PROXY_HOPS = int(os.environ.get("TRUSTED_PROXY_HOPS", "1"))


def _client_ip(request: Request) -> str:
    # X-Forwarded-For is "client, proxy1, proxy2, ...". The LEFTMOST entry is
    # whatever the client sent and is fully spoofable, so we must NOT trust it.
    # The rightmost entries are appended by infrastructure we control; counting
    # in from the right by the number of trusted hops yields the real client IP
    # that our own proxy actually observed and cannot be forged by the client.
    xff = request.headers.get("x-forwarded-for")
    if xff:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            idx = len(parts) - TRUSTED_PROXY_HOPS
            if idx < 0:
                idx = 0  # header shorter than expected; fall back to leftmost
            return parts[idx]
    return request.client.host if request.client else "unknown"


class UnlockRequest(BaseModel):
    code: str


@app.post("/api/unlock")
def unlock(req: UnlockRequest, request: Request) -> JSONResponse:
    """Exchange the shared access code for the gate cookie.

    Restored after it was dropped in the multi-company upgrade while the lock
    screen that calls it stayed. Without this route the gate has no door: the
    middleware answers /api/unlock with 401 before routing, so the Unlock
    button reports "Incorrect code" no matter what is typed.

    Per-IP rate limiting only -- deliberately NOT the global backstop used by
    /api/login. A global counter on the gate would let anyone lock every user
    out of the whole deployment by burning it from throwaway addresses, and
    being unable to reach your own app is a worse outcome here than slowing a
    guesser who already has to defeat a random code.
    """
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
    if not effective_code:
        # No code configured anywhere. Say so rather than reporting a wrong
        # code -- otherwise this looks identical to a typo and sends whoever is
        # locked out hunting for a password that does not exist.
        return JSONResponse(
            {"ok": False, "detail": "No access code is configured for this deployment."},
            status_code=503,
        )

    # compare_digest, not ==, so a wrong code cannot be narrowed down by timing.
    if secrets.compare_digest(req.code or "", effective_code):
        _unlock_attempts.pop(ip, None)
        resp = JSONResponse({"ok": True})
        resp.set_cookie("cc_access", effective_code, max_age=60 * 60 * 24 * 30,
                        httponly=True, samesite="lax", secure=True)
        return resp

    attempts.append(now)
    _unlock_attempts[ip] = attempts
    # A run of these on the support page is the difference between "the app is
    # down" and "the code being typed is wrong" -- the exact ambiguity that cost
    # an afternoon. The code itself is never recorded, only that one was refused.
    support.record_note("unlock_failed",
                        f"Access code refused ({len(attempts)} of "
                        f"{UNLOCK_MAX_ATTEMPTS} attempts in the window)",
                        path="/api/unlock")
    return JSONResponse({"ok": False, "detail": "Incorrect code"}, status_code=401)


# ── PASSKEY (fingerprint / Face ID) SIGN-IN ──────────────────────────────────
# Additive to the access code, never a replacement: a successful passkey auth
# sets the SAME cc_access cookie /api/unlock sets, so the gate, identity scope,
# and Susan's chat history all behave exactly as if she had typed the code.
# A total WebAuthn failure of any kind leaves the code path byte-identical.

import webauthn as _webauthn
from webauthn.helpers import (
    base64url_to_bytes as _wa_b64d,
    bytes_to_base64url as _wa_b64e,
    options_to_json_dict as _wa_options_dict,
)
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

# One-time-use guard for auth challenges. Stateless signed tokens survive
# restarts by design, so replay inside the TTL is bounded here instead.
# Best-effort on a single Render instance; the token TTL is the hard cap.
_used_passkey_challenges: Dict[str, float] = {}
_PASSKEY_CHALLENGE_TTL = 180


def _rp_and_origin(request: Request) -> tuple:
    """(rp_id, expected_origins) for this request's host. .onrender.com is on
    the Public Suffix List, so the full hostname is a valid rp_id."""
    host = request.url.hostname or "localhost"
    if host in ("localhost", "127.0.0.1"):
        return host, [f"http://{host}:{request.url.port or 8137}",
                      f"http://{host}:8000", f"http://{host}:8137"]
    return host, [f"https://{host}"]


def _challenge_already_used(challenge_b64: str) -> bool:
    now = time.time()
    for k in [k for k, t in _used_passkey_challenges.items()
              if now - t > _PASSKEY_CHALLENGE_TTL]:
        _used_passkey_challenges.pop(k, None)
    if challenge_b64 in _used_passkey_challenges:
        return True
    _used_passkey_challenges[challenge_b64] = now
    return False


@app.get("/api/passkey/enabled")
def passkey_enabled(request: Request) -> JSONResponse:
    """Gate-exempt. Should the lock page show the fingerprint button?
    Never errors -- 'no button' is the safe degradation."""
    if not crm.is_configured():
        return JSONResponse({"enabled": False})
    rp_id, _ = _rp_and_origin(request)
    return JSONResponse({"enabled": passkeys.enabled_cached(rp_id)})


@app.post("/api/passkey/auth-options")
def passkey_auth_options(request: Request) -> JSONResponse:
    """Gate-exempt. Start a fingerprint sign-in: challenge + allowed credentials.

    Same per-IP rate window as /api/unlock, and deliberately NO global
    backstop -- a global counter on the gate would let anyone lock Susan out
    of her own app from throwaway addresses (see the unlock docstring).
    """
    ip = _client_ip(request)
    now = time.time()
    attempts = [t for t in _unlock_attempts.get(ip, []) if now - t < UNLOCK_WINDOW_SECONDS]
    if len(attempts) >= UNLOCK_MAX_ATTEMPTS:
        wait_min = max(1, int((UNLOCK_WINDOW_SECONDS - (now - attempts[0])) / 60) + 1)
        return JSONResponse(
            {"ok": False, "detail": f"Too many attempts. Try again in about {wait_min} minute(s)."},
            status_code=429,
        )
    rp_id, _ = _rp_and_origin(request)
    try:
        creds = passkeys.list_credentials(rp_id)
    except passkeys.PasskeyStoreUnavailable:
        return JSONResponse(
            {"ok": False, "detail": "Fingerprint sign-in is temporarily unavailable -- "
                                    "your access code still works."},
            status_code=503,
        )
    if not creds:
        return JSONResponse(
            {"ok": False, "detail": "No fingerprint is set up yet -- use your access code."},
            status_code=404,
        )
    opts = _webauthn.generate_authentication_options(
        rp_id=rp_id,
        timeout=120000,
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=passkeys._b64u_dec(c["CredentialID"]))
            for c in creds
        ],
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    state = signed_tokens.mint("webauthn-auth",
                               {"challenge": _wa_b64e(opts.challenge), "rp": rp_id},
                               ttl_seconds=_PASSKEY_CHALLENGE_TTL)
    return JSONResponse({"state": state, "options": _wa_options_dict(opts)})


class PasskeyAuthRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    state: str
    credential: dict


@app.post("/api/passkey/auth")
def passkey_auth(req: PasskeyAuthRequest, request: Request) -> JSONResponse:
    """Gate-exempt. Finish a fingerprint sign-in: verify the assertion, set the
    SAME gate cookie /api/unlock sets. Failures are rate-limited like wrong
    codes; a store outage is a 503, never a 'wrong fingerprint', and is never
    charged to the limiter (the users.UserLookupUnavailable lesson)."""
    ip = _client_ip(request)
    now = time.time()
    attempts = [t for t in _unlock_attempts.get(ip, []) if now - t < UNLOCK_WINDOW_SECONDS]
    if len(attempts) >= UNLOCK_MAX_ATTEMPTS:
        wait_min = max(1, int((UNLOCK_WINDOW_SECONDS - (now - attempts[0])) / 60) + 1)
        return JSONResponse(
            {"ok": False, "detail": f"Too many attempts. Try again in about {wait_min} minute(s)."},
            status_code=429,
        )

    def _fail(detail: str, status: int = 401) -> JSONResponse:
        attempts.append(now)
        _unlock_attempts[ip] = attempts
        support.record_note("passkey_failed", detail, path="/api/passkey/auth")
        return JSONResponse({"ok": False, "detail": detail}, status_code=status)

    rp_id, origins = _rp_and_origin(request)
    body = signed_tokens.verify("webauthn-auth", req.state)
    if body is None or body.get("rp") != rp_id:
        return _fail("Sign-in expired -- tap the fingerprint button again. "
                     "Your access code always works too.")
    challenge_b64 = body.get("challenge") or ""
    if _challenge_already_used(challenge_b64):
        return _fail("That sign-in was already used -- tap the fingerprint button again.")

    cred_id = str(req.credential.get("rawId") or req.credential.get("id") or "")
    try:
        stored = passkeys.get_credential(cred_id)
    except passkeys.PasskeyStoreUnavailable:
        # NOT counted against the limiter: nothing is wrong with her fingerprint.
        return JSONResponse(
            {"ok": False, "detail": "Fingerprint sign-in is temporarily unavailable -- "
                                    "your access code still works."},
            status_code=503,
        )
    if not stored:
        return _fail("This device's fingerprint isn't set up here -- use your access code, "
                     "then add it again from Settings.")

    try:
        ver = _webauthn.verify_authentication_response(
            credential=req.credential,
            expected_challenge=_wa_b64d(challenge_b64),
            expected_rp_id=rp_id,
            expected_origin=origins,
            credential_public_key=passkeys._b64u_dec(stored["PublicKey"]),
            credential_current_sign_count=int(stored.get("SignCount") or 0),
            require_user_verification=True,
        )
    except Exception as e:
        return _fail("Fingerprint didn't verify -- your access code always works. "
                     f"({type(e).__name__})")

    effective_code = get_access_code()
    if not effective_code:
        # A cookie the gate can't match must not be minted (mirrors /api/unlock).
        return JSONResponse(
            {"ok": False, "detail": "No access code is configured for this deployment."},
            status_code=503,
        )
    passkeys.touch(stored["record_id"], ver.new_sign_count)
    _unlock_attempts.pop(ip, None)
    resp = JSONResponse({"ok": True})
    resp.set_cookie("cc_access", effective_code, max_age=60 * 60 * 24 * 30,
                    httponly=True, samesite="lax", secure=True)
    return resp


class PasskeyRegisterRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    state: str
    label: str = ""
    credential: dict


@app.post("/api/passkey/register-options")
def passkey_register_options(request: Request) -> JSONResponse:
    """GATED (middleware) + owner check: only someone already inside may add a
    fingerprint. Registration from outside would be a permanent skeleton key."""
    if not _is_owner_request(request):
        return JSONResponse({"ok": False, "detail": "Owner access required"}, status_code=403)
    if not crm.is_configured():
        return JSONResponse({"ok": False, "detail": "Airtable must be connected first."},
                            status_code=503)
    rp_id, _ = _rp_and_origin(request)
    try:
        existing = passkeys.list_credentials(rp_id)
    except passkeys.PasskeyStoreUnavailable:
        return JSONResponse({"ok": False, "detail": "Storage is temporarily unreachable -- "
                                                    "try again in a minute."}, status_code=503)
    opts = _webauthn.generate_registration_options(
        rp_id=rp_id,
        rp_name="The Dreamerie Command Center",
        user_id=passkeys.get_user_handle(),
        user_name="dreamerie-owner",
        user_display_name="Dreamerie Owner",
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=passkeys._b64u_dec(c["CredentialID"]))
            for c in existing
        ],
        timeout=120000,
    )
    state = signed_tokens.mint("webauthn-reg",
                               {"challenge": _wa_b64e(opts.challenge), "rp": rp_id},
                               ttl_seconds=300)
    return JSONResponse({"state": state, "options": _wa_options_dict(opts)})


@app.post("/api/passkey/register")
def passkey_register(req: PasskeyRegisterRequest, request: Request) -> JSONResponse:
    """GATED + owner check. Store the new device credential."""
    if not _is_owner_request(request):
        return JSONResponse({"ok": False, "detail": "Owner access required"}, status_code=403)
    rp_id, origins = _rp_and_origin(request)
    body = signed_tokens.verify("webauthn-reg", req.state)
    if body is None or body.get("rp") != rp_id:
        return JSONResponse({"ok": False, "detail": "Setup expired -- tap Add again."},
                            status_code=401)
    try:
        ver = _webauthn.verify_registration_response(
            credential=req.credential,
            expected_challenge=_wa_b64d(body.get("challenge") or ""),
            expected_rp_id=rp_id,
            expected_origin=origins,
            require_user_verification=True,
        )
    except Exception as e:
        return JSONResponse({"ok": False,
                             "detail": f"Couldn't verify this device ({type(e).__name__})."},
                            status_code=400)
    transports = req.credential.get("response", {}).get("transports") or []
    label = (req.label or "This device").strip()[:40]
    try:
        passkeys.add_credential(
            cred_id_b64=_wa_b64e(ver.credential_id),
            public_key_b64=_wa_b64e(ver.credential_public_key),
            sign_count=ver.sign_count,
            label=label,
            rp_id=rp_id,
            transports=json.dumps(transports)[:200],
        )
    except passkeys.PasskeyStoreUnavailable:
        return JSONResponse({"ok": False, "detail": "Storage is temporarily unreachable -- "
                                                    "try again in a minute."}, status_code=503)
    return JSONResponse({"ok": True, "id": _wa_b64e(ver.credential_id), "label": label})


@app.get("/api/passkey/credentials")
def passkey_credentials(request: Request) -> JSONResponse:
    """GATED + owner check. Devices enrolled -- labels and dates only, never keys."""
    if not _is_owner_request(request):
        return JSONResponse({"ok": False, "detail": "Owner access required"}, status_code=403)
    rp_id, _ = _rp_and_origin(request)
    try:
        creds = passkeys.list_credentials(rp_id)
    except passkeys.PasskeyStoreUnavailable:
        return JSONResponse({"ok": False, "detail": "Storage is temporarily unreachable."},
                            status_code=503)
    return JSONResponse({"ok": True, "credentials": [
        {"id": c["CredentialID"], "label": c.get("Label", "Device"),
         "created_at": c.get("CreatedAt", ""), "last_used": c.get("LastUsedAt", "")}
        for c in creds
    ]})


@app.delete("/api/passkey/credentials/{cred_id}")
def passkey_delete(cred_id: str, request: Request) -> JSONResponse:
    """GATED + owner check. Remove a device. Removing the last one is fine --
    the access code always works."""
    if not _is_owner_request(request):
        return JSONResponse({"ok": False, "detail": "Owner access required"}, status_code=403)
    try:
        ok = passkeys.delete_credential(cred_id)
    except passkeys.PasskeyStoreUnavailable:
        return JSONResponse({"ok": False, "detail": "Storage is temporarily unreachable."},
                            status_code=503)
    if not ok:
        return JSONResponse({"ok": False, "detail": "Device not found"}, status_code=404)
    return JSONResponse({"ok": True, "note": "Your access code still works."})


@app.post("/api/login")
def login(req: LoginRequest, request: Request) -> JSONResponse:
    """Authenticate with username + password, return signed session token in cookie."""
    ip = _client_ip(request)
    now = time.time()
    global _global_unlock_attempts

    # Per-IP sliding-window lockout.
    attempts = [t for t in _unlock_attempts.get(ip, []) if now - t < UNLOCK_WINDOW_SECONDS]
    # Global sliding-window backstop (defeats IP rotation / X-Forwarded-For spoofing).
    _global_unlock_attempts = [t for t in _global_unlock_attempts if now - t < UNLOCK_WINDOW_SECONDS]

    if len(attempts) >= UNLOCK_MAX_ATTEMPTS or len(_global_unlock_attempts) >= UNLOCK_GLOBAL_MAX_ATTEMPTS:
        basis = attempts if len(attempts) >= UNLOCK_MAX_ATTEMPTS else _global_unlock_attempts
        wait_min = max(1, int((UNLOCK_WINDOW_SECONDS - (now - basis[0])) / 60) + 1)
        return JSONResponse(
            {"ok": False, "detail": f"Too many attempts. Try again in about {wait_min} minute(s)."},
            status_code=429,
        )

    # Look up user. A datastore outage must NOT be reported as a wrong password:
    # doing so charged the attempt to the lockout above and to the
    # deployment-wide backstop, so a few seconds of trouble locked real people
    # out for fifteen minutes while telling them their credentials were wrong.
    # Fail loudly and charge them nothing.
    try:
        user = users.lookup_user(req.username)
    except users.UserLookupUnavailable:
        log.error("Login blocked: user store unreachable")
        return JSONResponse(
            {"ok": False, "detail": "Sign-in is temporarily unavailable. Nothing is wrong with "
                                    "your password -- please try again in a minute."},
            status_code=503,
        )
    if not user or not users.verify_password(req.password, user["password_hash"]):
        attempts.append(now)
        _unlock_attempts[ip] = attempts
        _global_unlock_attempts.append(now)
        return JSONResponse({"ok": False, "detail": "Invalid username or password"}, status_code=401)

    # The shipped default credential (owner/changeme) is public knowledge -- it
    # is printed in this repo's history. If it still verifies, that is not a
    # login, it is an open door: refuse it, say why, and flag it on /support.
    # A real owner is never locked out by this -- Susan uses the access code,
    # and any legitimately-set password different from "changeme" passes.
    if req.username == "owner" and req.password == "changeme":
        support.record_note(
            "default_credential",
            "A login using the PUBLISHED default credential (owner/changeme) "
            "was refused. Delete or repassword the 'owner' account in "
            "Settings → Users.", path="/api/login")
        return JSONResponse(
            {"ok": False, "detail": "This account still has the factory default password, "
                                    "which is published and therefore disabled. Reset it from "
                                    "Settings → Users on an owner login, or use the access code."},
            status_code=403,
        )

    # Successful login: clear this IP's attempts (leave the global counter alone
    # so one valid login can't reset a backstop an attacker is filling).
    _unlock_attempts.pop(ip, None)
    users.update_last_login(req.username)
    token = users.create_session_token(req.username)
    resp = JSONResponse({"ok": True})
    max_age = 60 * 60 * 24 * 30 if req.remember_me else None
    resp.set_cookie("cc_session", token, max_age=max_age, httponly=True, samesite="lax", secure=True)
    return resp


@app.post("/api/logout")
def logout(request: Request) -> JSONResponse:
    """Clear session cookie."""
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("cc_session")
    resp.delete_cookie("cc_access")  # Also clear old-style cookie if present
    return resp


@app.get("/api/me")
def get_current_user(request: Request) -> JSONResponse:
    """Owner-only. Return info about the currently logged-in user."""
    session_token = request.cookies.get("cc_session")
    if not session_token:
        # No per-user session. On an access-code deployment that is normal, not
        # an error: report a benign identity so the UI shows a signed-in state
        # instead of a "session expired" banner. access_mode tells the frontend
        # not to expect a per-user account. This grants no backend privilege --
        # owner-only endpoints check the session role, which is still absent.
        if _access_authenticated(request):
            return JSONResponse({"ok": True, "username": None, "role": None,
                                 "access_mode": True})
        return JSONResponse({"username": None}, status_code=401)
    username = users.verify_session_token(session_token)
    if not username:
        if _access_authenticated(request):
            return JSONResponse({"ok": True, "username": None, "role": None,
                                 "access_mode": True})
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
        "is_superadmin": _is_superadmin(request),
    })


def _authed_username(request: Request) -> Optional[str]:
    """Return the logged-in username from the session cookie, or None if
    unauthenticated (local dev with no gate, or the legacy ACCESS_CODE-only
    cookie with no real account behind it)."""
    token = request.cookies.get("cc_session")
    if not token:
        return None
    return users.verify_session_token(token)


# A stable identity for a deployment that authenticates with the shared access
# code instead of per-user logins (Susan's build has no login form at all -- the
# only way in is the code). On such a deployment the access cookie IS the
# identity: there is exactly one, so there is no cross-account bleed to guard
# against. Treating it as "not authenticated" is what 401'd /api/history and
# /api/me and showed a correctly-signed-in user a permanent "session expired,
# sign in again" banner -- with no login form to sign in to.
_ACCESS_SCOPE = "access"


def _access_authenticated(request: Request) -> bool:
    """True if the request carries a valid access-code cookie. Mirrors the
    access_gate's own cc_access check, so it can never disagree with the gate
    about whether this request is legitimately inside."""
    code = get_access_code()
    if not code:
        return False
    return secrets.compare_digest(request.cookies.get("cc_access", ""), code)


def _identity_scope(request: Request) -> Optional[str]:
    """The bucket prefix for whoever is asking, or None if we truly cannot say.

      per-user session  -> "user:<name>"   (isolated per account)
      access-code only  -> "access"        (one shared identity, by design)
      neither           -> None            (refuse to guess)

    A per-user session wins when present, so a real multi-account deployment
    keeps full isolation and only an access-code-only deployment ever lands on
    the shared bucket."""
    username = _authed_username(request)
    if username:
        return f"user:{username}"
    if _access_authenticated(request):
        return _ACCESS_SCOPE
    return None


def _scoped_chat_id(request: Request, chat_id: str) -> str:
    """Namespace chat_id by the logged-in account so two different logins
    never read or write each other's conversation history -- e.g. if Vinny
    hands his mother a separate account, her conversation with Annabelle
    stays hers, and never bleeds into his own history/context on reload.
    Falls back to the raw chat_id (the old shared "default" bucket) only when
    there's no authenticated session at all.

    THE FALLBACK IS THE "I DON'T REMEMBER ANYTHING" BUG. The access gate lets a
    request through on the legacy `cc_access` cookie even when `cc_session` is
    gone (see access_gate). This function only looks at `cc_session`, so such a
    request is served happily -- but against the raw "default" bucket instead of
    "user:<name>:default". Different bucket, empty history, no error anywhere.

    The trigger is ordinary: logging in without "remember me" sets cc_session
    with max_age=None, a browser-session cookie that dies when the browser
    closes, while cc_access survives. Reopen the app and memory looks wiped.

    Callers that touch per-user memory must use _scoped_chat_id_checked()
    instead, which refuses to guess. This one stays for non-memory paths."""
    scope = _identity_scope(request)
    if scope is None:
        log.warning("MEMORY_SCOPE_FALLBACK chat_id=%s -- no session or access "
                    "cookie; history would resolve to the unscoped bucket", chat_id)
        return chat_id
    return f"{scope}:{chat_id}"


def _scoped_chat_id_checked(request: Request, chat_id: str) -> Optional[str]:
    """Scoped chat id, or None when we cannot say who is asking.

    Returning None is the point. Serving a different bucket silently is how a
    client demo ended with Annabelle saying she had no memory; making the caller
    handle "I don't know who you are" turns a silent data problem into an
    honest, fixable "your session expired, sign in again"."""
    scope = _identity_scope(request)
    if scope is None:
        return None
    return f"{scope}:{chat_id}"


class FileAttachment(BaseModel):
    name: str = ""
    type: str = ""
    size: int = 0
    data: str = ""  # base64, no data: prefix


class ChatRequest(BaseModel):
    message: str
    history: List[Dict[str, str]] = []  # [{"role": "user"|"assistant", "content": "..."}]
    mode: str = "combined"  # dreamerie | suzy_d | bear_arms | peptides | combined
    chat_id: str = "default"
    speaker: str = ""  # who's currently talking under this login, e.g. "Jane"; "" = the account owner
    file: Optional[FileAttachment] = None
    request_id: str = ""  # client-generated id; lets /api/chat/stop cancel this exact turn
    model: str = ""  # MODEL_CHOICES slug; "" or "auto" = the account default

    def clean(self) -> "ChatRequest":
        """Enforce size limits: cap message at 12k chars, history at last 30 turns."""
        self.message = self.message[:12000]
        self.history = self.history[-30:]
        self.chat_id = self.chat_id[:100]
        self.speaker = self.speaker[:80]
        self.request_id = self.request_id[:100]
        self.model = self.model[:40]
        if self.file and len(self.file.data) > 7_000_000:  # ~5MB decoded, base64-inflated
            self.file = None
        return self


class ChatResponse(BaseModel):
    reply: str
    delegated_to: List[str] = []
    artifact_url: Optional[str] = None  # URL to a created artifact (proposal, strategy, etc.)
    artifact_title: Optional[str] = None  # Human-readable title for the artifact
    alert: Optional[Dict[str, str]] = None  # {"title": ...} -- surfaces as a floating notification card
    speaker_name: Optional[str] = None  # who's currently tagged as speaking under this login ("" = owner)
    model_used: Optional[str] = None  # human-readable label of the model that answered
    # Per-stage timing breakdown in seconds (precheck = Airtable cap/count
    # round-trips before the first model call; model = Anthropic calls;
    # tools = tool execution; save = post-reply Airtable writes). Added to
    # diagnose response delay with real numbers instead of guesses.
    timings: Dict[str, float] = {}


_ALERT_RE = re.compile(r"^\s*\[\[ALERT:\s*(.+?)\s*\]\]\s*\n?", re.IGNORECASE)


def _extract_alert(text: str) -> tuple[str, Optional[Dict[str, str]]]:
    """Strip a leading [[ALERT: Title]] marker from a reply, if present.

    Returns (cleaned_reply, alert_dict_or_None). Applied to owner-persona
    replies only -- the public widget's system prompt never teaches this
    marker, so it can't leak there even if this runs on both paths.
    """
    m = _ALERT_RE.match(text)
    if not m:
        return text, None
    title = m.group(1)[:80]
    cleaned = text[m.end():].strip()
    return cleaned, {"title": title, "body": cleaned[:160]}


_WRITING_AGENTS = {"proposal_writer", "audit_writer", "proposal_reviewer"}

def call_sub_agent(agent_key: str, query: str, model: str = "") -> str:
    """Run one sub-agent with a fresh, isolated context and return its answer.

    Runs on whatever model the turn is using, so picking Opus for a proposal
    also upgrades the proposal_writer that actually drafts it.
    """
    agent = SUB_AGENTS[agent_key]
    max_tokens = 2048 if agent_key in _WRITING_AGENTS else 1024
    resp = client.messages.create(
        model=model or resolve_model(),
        max_tokens=max_tokens,
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


def run_web_search(query: str, model: str = "") -> tuple:
    """Run one live web search via Anthropic's server-side search tool.
    Returns (answer_text, number_of_searches_actually_performed)."""
    resp = client.messages.create(
        model=model or resolve_model(),
        max_tokens=1024,
        tools=[WEB_SEARCH_TOOL],
        messages=[{"role": "user", "content": f"Search the web and answer concisely: {query}"}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    return text, _count_web_searches(resp.content)


_SCRAPE_MAX_BYTES = 600_000
_SCRAPE_MAX_TEXT = 8000


def scrape_page_text(url: str) -> str:
    """Fetch a public web page and return its readable text (title, meta
    description, visible body text). Refuses non-HTTP schemes and hosts that
    resolve to private/internal addresses; truncates long pages. Fetch
    failures return a plain-language explanation instead of raising -- a dead
    domain or broken certificate is a finding Annabelle should report."""
    resp, err = webfetch.safe_get(url)
    if err:
        return err
    html = resp.content[:_SCRAPE_MAX_BYTES].decode(resp.encoding or "utf-8", errors="replace")
    title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    desc_m = re.search(
        r'<meta[^>]+name=["\']description["\'][^>]*content=["\'](.*?)["\']', html, re.I | re.S
    )
    text = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    import html as _html
    text = re.sub(r"\s+", " ", _html.unescape(text)).strip()
    parts = [f"URL: {resp.url}", f"HTTP STATUS: {resp.status_code}"]
    if title_m:
        parts.append("TITLE: " + re.sub(r"\s+", " ", title_m.group(1)).strip()[:300])
    if desc_m:
        parts.append("META DESCRIPTION: " + desc_m.group(1).strip()[:500])
    if text:
        truncated = " (truncated)" if len(text) > _SCRAPE_MAX_TEXT else ""
        parts.append(f"PAGE TEXT{truncated}:\n" + text[:_SCRAPE_MAX_TEXT])
    else:
        parts.append(
            "PAGE TEXT: none extracted -- likely a JavaScript-rendered page. "
            "Only the title/metadata above is available."
        )
    return "\n".join(parts)


_TEXT_FILE_TYPES = {
    "text/plain", "text/csv", "application/json", "text/html",
    "application/xml", "text/xml", "text/markdown",
}


def _build_user_content(user_message: str, file: Optional["FileAttachment"]):
    """Return the `content` value for the first user message. Plain string if
    no file; a content-block list if a file is attached (image -> vision
    block, text-like -> decoded and inlined, anything else -> named but
    flagged as unreadable so Annabelle says so instead of ignoring it)."""
    if not file or not file.data:
        return user_message
    if file.type.startswith("image/"):
        return [
            {"type": "text", "text": user_message},
            {"type": "image", "source": {"type": "base64", "media_type": file.type, "data": file.data}},
        ]
    if file.type in _TEXT_FILE_TYPES:
        try:
            import base64 as _b64
            decoded = _b64.b64decode(file.data).decode("utf-8", errors="replace")[:15000]
        except Exception:
            decoded = "(couldn't decode this file's contents)"
        return (
            f"{user_message}\n\n[Attached file: {file.name} ({file.type})]\n{decoded}"
        )
    return (
        f"{user_message}\n\n[Vinny attached a file named {file.name} ({file.type or 'unknown type'}) "
        "-- you can't read this file format yet, so tell him plainly rather than guessing at its contents.]"
    )


# ---- Stop button: lets the client cancel an in-flight run_main_brain loop --
# The chat call isn't token-streamed (Claude's reply arrives as one block),
# so "stop" can't interrupt mid-generation. What it CAN do -- and the case
# that actually matters, since a single turn may loop through several
# Anthropic calls plus slow tools (deep research, video generation) -- is
# stop the loop from starting another round once the user's given up on it.
_cancelled_requests: set = set()
_cancel_lock = threading.Lock()

# Pending email drafts: keyed by scoped chat_id so "send it" in the next turn
# can retrieve the exact to/subject/body without the model re-drafting.
_pending_email_drafts: Dict[str, dict] = {}


def _mark_cancelled(request_id: str) -> None:
    if not request_id:
        return
    with _cancel_lock:
        _cancelled_requests.add(request_id)


def _is_cancelled(request_id: str) -> bool:
    if not request_id:
        return False
    with _cancel_lock:
        return request_id in _cancelled_requests


def _clear_cancelled(request_id: str) -> None:
    if not request_id:
        return
    with _cancel_lock:
        _cancelled_requests.discard(request_id)


STOPPED_REPLY = "(stopped)"


def _run_main_brain_events(user_message: str, history: List[Dict[str, str]],
                   system_prompt: str = MAIN_BRAIN_SYSTEM_PROMPT,
                   tools=DELEGATION_TOOLS, enable_search: bool = False,
                   persona: str = "owner", file: Optional["FileAttachment"] = None,
                   request_id: str = "", model: str = "", chat_id: str = "",
                   business: str = ""):
    """The Main Brain as a stream of events rather than one blocking call.

    `business` is the active chat mode. It reaches the dispatch loop so that
    outbound actions can use the right brand's identity -- the mode used to
    select only the tool list and the prompt, which meant Annabelle answering
    as Bear Arms still sent mail from The Dreamerie's address.

    Yields, in order:
      {"type": "text",  "text": ...}   token deltas, the moment they arrive
      {"type": "tool",  "name": ...}   a sub-agent/tool started (for the live log)
      {"type": "done",  "response": ChatResponse}  final, with timings + artifacts

    run_main_brain() below drains this and returns just the ChatResponse, so
    every existing non-streaming caller is unchanged. Splitting it this way
    means the big tool-dispatch block below exists in exactly one place.
    """
    timings: Dict[str, float] = {"precheck": 0.0, "model": 0.0, "tools": 0.0}
    _t0 = time.perf_counter()
    # Resolved once per turn: every model call this turn makes (main brain,
    # sub-agents, web search) uses the same one, so a turn can't half-answer on
    # Opus and half on Haiku.
    active_model = resolve_model(model)
    model_label = next((s["label"] for s in MODEL_CHOICES.values() if s["id"] == active_model), active_model)
    # Hard spend circuit breaker: checked BEFORE any Anthropic call is made,
    # so a capped persona costs nothing to refuse -- not just a polite
    # after-the-fact message once money's already been spent.
    cap = get_public_chat_cap() if persona == "public" else get_owner_chat_cap()
    capped_reply = PUBLIC_CAPPED_REPLY if persona == "public" else OWNER_CAPPED_REPLY
    if crm.get_chat_count(persona) >= cap:
        yield {"type": "done", "response": ChatResponse(reply=capped_reply, delegated_to=[])}
        return
    # Fire-and-forget: the count write shouldn't block the reply. The cap
    # check above reads the cached snapshot, and set_setting updates that
    # cache in place, so this instance still counts accurately.
    threading.Thread(target=crm.increment_chat_count, args=(persona,), daemon=True).start()

    # Keep ONLY role+content on the way to the API. History round-trips through
    # the browser, and get_history() adds a "speaker" field for the UI badge --
    # Anthropic 400s on any extra key ("messages.0.speaker: Extra inputs are
    # not permitted"), which surfaced as a 500 on EVERY message in any
    # conversation reloaded after sign-in. Whitelisting here (not blacklisting
    # "speaker") means the next UI-only field can't re-create the bug.
    messages = [
        {"role": m.get("role") if m.get("role") in ("user", "assistant") else "user",
         "content": m.get("content", "")}
        for m in history if m.get("content")
    ] + [{"role": "user", "content": _build_user_content(user_message, file)}]
    delegated_to: List[str] = []
    artifact_url: Optional[str] = None
    artifact_title: Optional[str] = None
    speaker_name: Optional[str] = None  # set only if set_speaker fires this turn; None = unchanged

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

    # Everything she says across every round, in order. A turn that calls a
    # tool often narrates first ("Let me pull that up") -- that narration is
    # spoken while the tool runs instead of being thrown away, which is most
    # of the perceived speed-up on tool-using turns.
    spoken_so_far: List[str] = []

    # Loop to allow multiple rounds of tool use (e.g. two sub-agents needed).
    for _ in range(4):
        if _is_cancelled(request_id):
            yield {"type": "done", "response": ChatResponse(reply=STOPPED_REPLY, delegated_to=delegated_to,
                                 timings=timings, speaker_name=speaker_name)}
            return
        _tm = time.perf_counter()
        # Streamed, so the first sentence reaches the browser (and the TTS
        # engine) while the rest is still being generated, instead of the
        # client waiting on the whole completion before it can do anything.
        # Long-form content (blog posts, proposals, campaigns) needs more room.
        # Detect if the user is asking for content generation so we give enough tokens.
        _long_form_keywords = ("blog", "post", "campaign", "write", "draft", "story", "content", "copy", "article")
        _is_long_form = any(kw in user_message.lower() for kw in _long_form_keywords)
        _stream_max_tokens = 4096 if _is_long_form else 1024
        with client.messages.stream(
            model=active_model,
            max_tokens=_stream_max_tokens,
            system=effective_system_prompt,
            tools=effective_tools,
            messages=messages,
        ) as stream:
            for delta in stream.text_stream:
                if _is_cancelled(request_id):
                    break
                spoken_so_far.append(delta)
                yield {"type": "text", "text": delta}
            resp = stream.get_final_message()
        timings["model"] = round(timings["model"] + (time.perf_counter() - _tm), 3)

        if _is_cancelled(request_id):
            yield {"type": "done", "response": ChatResponse(reply=STOPPED_REPLY, delegated_to=delegated_to,
                                 timings=timings, speaker_name=speaker_name)}
            return

        n_searches = _count_web_searches(resp.content)
        if n_searches:
            crm.increment_search_count(n_searches)
            delegated_to.append("Web Search")

        if resp.stop_reason != "tool_use":
            # Everything streamed this turn IS the reply -- what was displayed,
            # what was spoken, and what gets saved to memory all match.
            final_text = "".join(spoken_so_far)
            final_text, alert = _extract_alert(final_text) if persona == "owner" else (final_text, None)
            if alert:
                # Fire-and-forget: also ring the owner's phone (lock-screen
                # notification), not just the in-app floating card, in case
                # they've stepped away from the screen.
                threading.Thread(
                    target=push.send_to_owner, args=(alert["title"], alert["body"]), daemon=True
                ).start()
            timings["total"] = round(time.perf_counter() - _t0, 3)
            yield {"type": "done", "response": ChatResponse(reply=final_text, delegated_to=delegated_to,
                                 timings=timings, artifact_url=artifact_url, artifact_title=artifact_title,
                                 alert=alert, speaker_name=speaker_name, model_used=model_label)}
            return

        # Assistant turn included tool_use block(s); append it, then run each
        # tool and append the results, then loop back to let the Main Brain
        # compose its final answer.
        messages.append({"role": "assistant", "content": resp.content})
        _tt = time.perf_counter()
        tool_results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            if _is_cancelled(request_id):
                yield {"type": "done", "response": ChatResponse(reply=STOPPED_REPLY, delegated_to=delegated_to,
                                     timings=timings, speaker_name=speaker_name)}
                return
            yield {"type": "tool", "name": block.name}
            agent_key = TOOL_NAME_TO_AGENT_KEY.get(block.name)
            if block.name == "log_lead":
                delegated_to.append("CRM")
                answer = crm.create_lead(**block.input)
            elif block.name == "find_leads":
                delegated_to.append("CRM")
                answer = crm.list_leads(**block.input)
            elif block.name == "log_build_request":
                delegated_to.append("Build Queue")
                answer = crm.create_build_request(**block.input)
            elif block.name == "log_skill_note":
                delegated_to.append("Skills Log")
                answer = crm.log_skill_note(**block.input)
            elif block.name == "set_speaker":
                name = (block.input.get("name") or "").strip()[:80]
                speaker_name = name  # "" deliberately clears back to the owner
                answer = f"Tagging messages as {name} from now on." if name else "Back to the primary owner."
            elif block.name == "client_interview":
                delegated_to.append("Client Interview")
                from . import onboarding as _ob
                _act = (block.input.get("action") or "next").strip().lower()
                _client = block.input.get("client", "")
                # Without this the startup edition was unreachable: onboarding.py
                # defaults every signature to "established", so the three
                # startup-only questions could never be asked.
                _edition = (block.input.get("edition") or "established").strip().lower()
                if _act == "record":
                    _ok, _msg = _ob.record_answer(
                        _client, block.input.get("question_id", ""),
                        block.input.get("answer", ""))
                    if not _ok:
                        answer = _msg
                    else:
                        _nx = _ob.next_question(_client, _edition)
                        answer = (
                            f"Saved that answer word for word.\n\n"
                            + (f"All {_nx.get('total')} questions are done for {_client}. "
                               f"Say 'status' any time to see the profile."
                               if _nx.get("done") else
                               f"({_nx.get('answered')}/{_nx.get('total')} answered"
                               + (f", {_nx['remaining_required']} required left"
                                  if _nx.get("remaining_required") else "")
                               + f")\n\nNext question [{_nx.get('id')}]:\n{_nx.get('question')}")
                        )
                elif _act == "status":
                    answer = _ob.readiness(_client, _edition)
                elif _act == "build_persona":
                    _ok, _text = _ob.build_persona(_client, _edition)
                    if not _ok:
                        answer = _text
                    else:
                        _slug = crm.create_artifact(f"{_client} — assistant persona", _text)
                        answer = _text + (
                            f"\n\nSaved at /artifact/{_slug}" if _slug else
                            "\n\n(Not saved -- the artifact store isn't reachable.)")
                elif _act == "recall":
                    _r = _ob.recall(_client, block.input.get("question", ""))
                    if not _r["reachable"]:
                        answer = (f"I couldn't reach the client profile store, so I can't "
                                  f"tell you what {_client} said. That's different from "
                                  f"them not having said it.")
                    elif not _r["found"] and not _r["never_asked"]:
                        answer = (f"There's no interview on file for {_client} at all. "
                                  f"Nothing has been captured, so anything I told you "
                                  f"about them would be invented. Run the interview.")
                    else:
                        _parts = []
                        if _r["found"]:
                            _parts.append(
                                f"What {_client} actually said (quote this, don't "
                                f"rephrase it):\n\n" + "\n\n".join(
                                    f'Q: {h["question"]}\nA: "{h["answer"]}"'
                                    for h in _r["found"]))
                        if _r["never_asked"]:
                            _parts.append(
                                "NEVER ASKED — I have no answer for these, and I won't "
                                "guess:\n" + "\n".join(
                                    f'  - {n["question"]}' for n in _r["never_asked"]))
                        answer = "\n\n".join(_parts)
                        crm.log_verification(
                            f"{_client}: {block.input.get('question','')}",
                            "Ground Truth", source="client onboarding interview",
                            detail=(_r["found"][0]["answer"][:200] if _r["found"] else "no answer captured"))
                else:
                    _nx = _ob.next_question(_client, _edition)
                    if _nx.get("error"):
                        answer = _nx["error"]
                    elif _nx.get("done"):
                        answer = (f"The interview for {_client} is complete "
                                  f"({_nx['answered']}/{_nx['total']}). "
                                  f"Say 'status' for the full profile.")
                    else:
                        answer = (
                            f"Question {_nx['answered'] + 1} of {_nx['total']} for {_client}"
                            + (" (required)" if _nx.get("required") else " (optional)")
                            + f" [{_nx['id']}]:\n\n{_nx['question']}"
                        )
            elif block.name == "draft_email":
                delegated_to.append("Email (draft)")
                to = block.input.get("to", "")
                subject = block.input.get("subject", "")
                body_text = block.input.get("body", "")
                if chat_id:
                    _pending_email_drafts[chat_id] = {"to": to, "subject": subject, "body": body_text}
                answer = f"DRAFT -- To: {to} | Subject: {subject}\n\n{body_text}"
            elif block.name == "send_email":
                delegated_to.append("Email (sent)")
                to = block.input.get("to", "")
                subject = block.input.get("subject", "")
                body_text = block.input.get("body", "")
                # If model called send_email with empty params, pull from pending draft
                if chat_id and (not to or not subject or not body_text):
                    pending = _pending_email_drafts.get(chat_id, {})
                    to = to or pending.get("to", "")
                    subject = subject or pending.get("subject", "")
                    body_text = body_text or pending.get("body", "")
                answer = emailer.send_email(to, subject, body_text, business=business)
                if chat_id:
                    _pending_email_drafts.pop(chat_id, None)
            elif block.name == "send_sms":
                delegated_to.append("SMS (sent)")
                from . import sms as _sms
                to_num = block.input.get("to", "")
                sms_body = block.input.get("body", "")
                result = _sms.send(to_num, sms_body)
                if result.get("sid"):
                    answer = f"SMS sent to {to_num} (SID: {result['sid']})"
                elif result.get("status") == "skipped":
                    answer = f"SMS skipped — Twilio not configured."
                else:
                    answer = f"SMS sent to {to_num}"
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
            elif block.name == "save_memory":
                delegated_to.append("Memory")
                answer = memory.add_memory(
                    block.input.get("summary", ""),
                    block.input.get("content", ""),
                    block.input.get("tags", ""),
                    block.input.get("source", ""),
                )
            elif block.name == "recall_memory":
                delegated_to.append("Memory")
                answer = memory.recall_memory(
                    block.input.get("query", ""),
                    block.input.get("tag", ""),
                )
            elif block.name == "generate_image":
                delegated_to.append("Image Generation")
                if crm.get_media_count("image") >= get_image_cap():
                    answer = IMAGE_CAPPED_REPLY
                else:
                    prompt = block.input.get("prompt", "")
                    result = media_gen.generate_image(prompt, block.input.get("aspect_ratio", ""))
                    if not result.get("ok"):
                        if result.get("error") == "not_connected":
                            answer = "Image generation isn't connected yet -- please set OPENAI_API_KEY or XAI_API_KEY in Settings."
                        elif result.get("error") == "chatgpt_not_connected":
                            answer = "ChatGPT isn't connected -- OPENAI_API_KEY needs to be set, or try Grok."
                        elif result.get("error") == "grok_not_connected":
                            answer = "Grok isn't connected -- XAI_API_KEY needs to be set, or try ChatGPT."
                        else:
                            answer = f"Image generation failed: {result.get('error')}"
                    else:
                        crm.increment_media_count("image")
                        name = prompt.strip()[:60] or "Generated image"
                        actual_provider = result.get("provider", "unknown")
                        save_result = assets.add_asset(name, result["url"], "Photo", "ai-generated",
                                                         f"Generated via {actual_provider}. Prompt: {prompt.strip()[:500]}")
                        if save_result.startswith("Saved "):
                            answer = (
                                f"Image generated via {actual_provider.upper()} and saved to the asset library as \"{name}\". "
                                f"Direct URL: {result['url']}"
                            )
                            # Create artifact record so frontend renders the URL in the Artifacts panel
                            artifact_slug = crm.create_artifact(name, f"![{name}]({result['url']})")
                            if artifact_slug:
                                artifact_url = f"/artifact/{artifact_slug}"
                                artifact_title = name
                        else:
                            answer = (
                                f"Image generated via {actual_provider.upper()}: {result['url']} "
                                f"(asset library save note: {save_result})"
                            )
            elif block.name == "generate_video":
                delegated_to.append("Video Generation")
                if crm.get_media_count("video") >= get_video_cap():
                    answer = VIDEO_CAPPED_REPLY
                else:
                    prompt = block.input.get("prompt", "")
                    result = media_gen.generate_video(
                        prompt,
                        block.input.get("duration", 8),
                        block.input.get("aspect_ratio", ""),
                        block.input.get("image_url", ""),
                    )
                    if not result.get("ok"):
                        if result.get("error") == "not_connected":
                            answer = "Video generation isn't connected yet -- XAI_API_KEY needs to be set."
                        elif result.get("error") == "timeout":
                            answer = "The video is still processing -- ask again in a minute, or check the asset library shortly."
                        else:
                            answer = f"Video generation failed: {result.get('error')}"
                    else:
                        crm.increment_media_count("video")
                        name = prompt.strip()[:60] or "Generated video"
                        save_result = assets.add_asset(name, result["url"], "Video", "ai-generated",
                                                         f"Generated via xAI. Prompt: {prompt.strip()[:500]}")
                        dur = result.get('duration', '')
                        if save_result.startswith("Saved "):
                            answer = f"Generated a {dur}s video and saved it to the asset library as \"{name}\"."
                            # Create artifact record so frontend renders the URL in the Artifacts panel
                            artifact_slug = crm.create_artifact(name, f"[Video]({result['url']})\n\nDuration: {dur}s")
                            if artifact_slug:
                                artifact_url = f"/artifact/{artifact_slug}"
                                artifact_title = name
                        else:
                            answer = f"Generated a {dur}s video: {result['url']} (asset library save did not confirm: {save_result})"
            elif block.name == "predict_video_cost":
                delegated_to.append("Video Cost Log")
                answer = video_cost.predict_cost(
                    block.input.get("project", ""),
                    block.input.get("predicted_cost", 0),
                    block.input.get("notes", ""),
                )
            elif block.name == "log_cost_checkpoint":
                delegated_to.append("Video Cost Log")
                answer = video_cost.log_checkpoint(
                    block.input.get("project", ""),
                    block.input.get("checkpoint", 0),
                    block.input.get("current_cost", 0),
                    block.input.get("notes", ""),
                )
            elif block.name == "log_actual_video_cost":
                delegated_to.append("Video Cost Log")
                answer = video_cost.log_actual(
                    block.input.get("project", ""),
                    block.input.get("actual_cost", 0),
                    block.input.get("lesson", ""),
                )
            elif block.name == "get_video_cost_accuracy":
                delegated_to.append("Video Cost Log")
                answer = video_cost.get_accuracy(block.input.get("project", ""))
            elif block.name == "set_agent_name":
                new_name = (block.input.get("name") or "").strip()
                ok = crm.set_setting(AGENT_NAME_KEY, new_name) if new_name else False
                delegated_to.append("Settings")
                answer = f"Saved -- you're now called {new_name}." if ok else "I heard the name but couldn't save it (settings store not connected)."
            elif block.name == "log_event":
                delegated_to.append("Events Tracker")
                answer = events.add_event(**block.input)
            elif block.name == "find_events":
                delegated_to.append("Events Tracker")
                answer = events.list_events(**block.input)
            elif block.name == "check_availability":
                delegated_to.append("Calendar")
                avail_date = block.input.get("date", "")
                result = gcal.check_availability(avail_date, business=business)
                if result.get("reason") == "Calendar not connected":
                    answer = (
                        "The calendar isn't connected yet, so I can't see the real "
                        "schedule for that day -- capture the customer's info and tell "
                        "them someone will confirm the timing. (Vinny: connect Google "
                        "Calendar in Settings to enable live availability.)"
                    )
                    crm.log_verification(avail_date, "Escalated", source="Google Calendar not connected")
                elif result.get("removals"):
                    slots = "; ".join(
                        f"{r['time']} in the {r['area']} area" for r in result["removals"]
                    )
                    avail = "still an open slot" if result["available"] else "fully booked (2 removals already)"
                    answer = f"On that day: {slots}. The day is {avail}."
                    crm.log_verification(avail_date, "Ground Truth", source="Google Calendar (live)", detail=answer)
                else:
                    answer = "No removals booked that day yet -- both slots are open."
                    crm.log_verification(avail_date, "Ground Truth", source="Google Calendar (live)", detail=answer)
            elif block.name == "create_removal_event":
                delegated_to.append("Calendar (booked)")
                answer = gcal.create_removal_event(
                    block.input.get("date", ""),
                    block.input.get("area", ""),
                    block.input.get("time", ""),
                    block.input.get("customer_name", ""),
                    block.input.get("customer_phone", ""),
                    business=business,
                )
            elif block.name == "create_inspection_event":
                delegated_to.append("Calendar (inspection booked)")
                answer = gcal.create_inspection_event(
                    block.input.get("date", ""),
                    block.input.get("area", ""),
                    block.input.get("time", ""),
                    block.input.get("visit_type", ""),
                    block.input.get("customer_name", ""),
                    block.input.get("customer_phone", ""),
                    business=business,
                )
            elif block.name == "get_vendor_events":
                delegated_to.append("Events Tracker")
                answer = vendor_events.list_upcoming_events(
                    block.input.get("status", ""),
                    block.input.get("search", ""),
                )
            elif block.name == "log_vendor_event":
                delegated_to.append("Events Tracker")
                answer = vendor_events.add_event(
                    block.input.get("event", ""),
                    block.input.get("date", ""),
                    block.input.get("time", ""),
                    block.input.get("location", ""),
                    block.input.get("fee", ""),
                    block.input.get("status", "TBD"),
                    block.input.get("action_needed", ""),
                    block.input.get("notes", ""),
                )
            elif block.name == "research_prospect":
                delegated_to.append("Prospect Research")
                company = block.input.get("company", "")
                industry = block.input.get("industry", "")
                search_queries = [
                    f'"{company}" {industry} website logo brand colors',
                    f'"{company}" reviews rating Google Yelp BBB Facebook Instagram',
                    f"top {industry} small business software CRM automation tools 2024",
                    f"{industry} small business pain points workflow challenges automation",
                ]
                search_parts = []
                searches_used = 0
                for sq in search_queries:
                    if crm.get_search_count() + searches_used < get_search_cap():
                        s_text, s_used = run_web_search(sq, active_model)
                        search_parts.append(f"SEARCH: {sq}\n{s_text}")
                        searches_used += s_used
                if searches_used:
                    crm.increment_search_count(searches_used)
                    delegated_to.append("Web Search")
                compiled = "\n\n---\n\n".join(search_parts) if search_parts else "No search results available."
                briefing = (
                    f"Research this prospect for Stinger Industries sales prep.\n\n"
                    f"COMPANY: {company}\nINDUSTRY: {industry}\n\n"
                    f"WEB SEARCH RESULTS:\n{compiled}"
                )
                raw = call_sub_agent("prospect_researcher", briefing, active_model)

                def _pf(label, text):
                    label_lower = label.lower()
                    for line in text.splitlines():
                        stripped = line.strip()
                        if stripped.lower().startswith(f"{label_lower}:"):
                            return stripped[len(label) + 1:].strip()
                    log.warning("Prospect parser: field %r not found in AI output", label)
                    return ""

                logo = _pf("LOGO_URL", raw)
                accent = _pf("ACCENT_COLOR", raw)
                website = _pf("WEBSITE", raw)
                save_result = prospects.create_prospect(
                    company=_pf("COMPANY", raw) or company,
                    industry=industry,
                    website="" if website.lower() in ("not found", "unknown") else website,
                    logo_url="" if logo.lower() in ("not found", "unknown") else logo,
                    accent="" if accent.lower() in ("not found", "unknown") else accent,
                    research_notes=(
                        _pf("INDUSTRY_SUMMARY", raw)
                        + "\n\nDigital footprint: " + (_pf("DIGITAL_FOOTPRINT", raw) or "none found")
                        + "\nSEO audit: " + (_pf("SEO_AUDIT", raw) or "unknown")
                        + "\nSales angle: " + (_pf("SALES_ANGLE", raw) or "")
                        + "\n\n" + _pf("SUMMARY", raw)
                    ).strip(),
                    competitive_notes=_pf("COMPETITIVE_NOTES", raw),
                    common_tools=_pf("COMMON_TOOLS", raw),
                    pain_points=_pf("PAIN_POINTS", raw),
                )
                slug = save_result.get("slug", "")
                demo_note = f"\n\nSaved to Prospects. Pre-branded demo ready at /demo/{slug}" if slug else ""
                answer = raw + demo_note
            elif block.name == "list_capabilities":
                delegated_to.append("Skill Toolbox")
                allowed = {t["name"] for t in tools}
                answer = toolbox.render_text(allowed, block.input.get("topic", ""))
            elif block.name == "scout_prospects":
                delegated_to.append("Prospect Scout")
                industry = block.input.get("industry", "")
                area = block.input.get("area") or "Port St. Lucie, FL"
                search_queries = [
                    f"best {industry} companies {area}",
                    f"{industry} {area} local family owned reviews rating",
                ]
                search_parts = []
                searches_used = 0
                for sq in search_queries:
                    if crm.get_search_count() + searches_used < get_search_cap():
                        s_text, s_used = run_web_search(sq, active_model)
                        search_parts.append(f"SEARCH: {sq}\n{s_text}")
                        searches_used += s_used
                if searches_used:
                    crm.increment_search_count(searches_used)
                    delegated_to.append("Web Search")
                if not search_parts:
                    answer = (
                        "Today's web-search cap is used up, so I can't scout new "
                        "prospects right now. Try again tomorrow or raise the cap in Settings."
                    )
                else:
                    compiled = "\n\n---\n\n".join(search_parts)
                    briefing = (
                        f"Scout local businesses for the Stinger Industries sales pipeline.\n\n"
                        f"INDUSTRY: {industry}\nAREA: {area}\n\n"
                        f"WEB SEARCH RESULTS:\n{compiled}"
                    )
                    answer = call_sub_agent("prospect_scout", briefing, active_model) + (
                        "\n\nWant the full research card on any of these? Say "
                        '"research <company name>" and I\'ll run it and save them to the pipeline.'
                    )
            elif block.name == "scrape_page":
                delegated_to.append("Page Scrape")
                answer = scrape_page_text(block.input.get("url", ""))
            elif block.name == "run_seo_audit":
                delegated_to.append("SEO Audit")
                answer = seo_audit.run_audit(block.input.get("url", ""))
            elif block.name == "list_prospects":
                delegated_to.append("Prospects Pipeline")
                answer = prospects.find_prospects(
                    search=block.input.get("search", ""),
                    status=block.input.get("status", ""),
                )
            elif block.name == "get_client_strategy":
                delegated_to.append("Client Strategy")
                client_name = block.input.get("client", "")
                rows = crm.get_strategy(client_name)
                if not rows:
                    # Say nothing is stored rather than let her fill the gap
                    # with a plausible-sounding strategy she invented.
                    answer = (
                        f"No stored strategy for '{client_name}'. Do not improvise one -- "
                        "tell the owner nothing is on file for this company and offer to "
                        "research them or have a strategy pushed in."
                    )
                else:
                    parts = []
                    for row in rows:
                        parts.append(
                            f"=== {row['client']} / {row['kind']} "
                            f"(priority: {row['priority']}, updated: {row['updated_at']}) ===\n"
                            f"{row['content']}"
                        )
                    answer = "\n\n".join(parts)
            elif block.name == "push_lead_to_hubspot":
                delegated_to.append("HubSpot CRM")
                if not os.environ.get("HUBSPOT_ACCESS_TOKEN"):
                    answer = "HubSpot isn't connected yet. Set HUBSPOT_ACCESS_TOKEN in Render environment variables. See HUBSPOT_SETUP_GUIDE.md for full instructions."
                else:
                    try:
                        result = hubspot.capture_lead(
                            name=block.input.get("name", ""),
                            email=block.input.get("email", ""),
                            phone=block.input.get("phone", ""),
                            company=block.input.get("company", ""),
                            service=block.input.get("service", ""),
                            notes=block.input.get("notes", ""),
                            deal_amount=block.input.get("deal_amount"),
                            deal_stage=block.input.get("deal_stage", ""),
                        )
                        answer = (
                            f"Pushed to HubSpot. Contact ID: {result['contact']['id']}, "
                            f"Deal ID: {result['deal']['id']}."
                        )
                    except Exception as e:
                        answer = f"HubSpot error: {e}"
            elif block.name == "search_hubspot_contact":
                delegated_to.append("HubSpot CRM")
                if not os.environ.get("HUBSPOT_ACCESS_TOKEN"):
                    answer = "HubSpot isn't connected yet. Set HUBSPOT_ACCESS_TOKEN in Render environment variables. See HUBSPOT_SETUP_GUIDE.md for full instructions."
                else:
                    try:
                        results = hubspot.search_contacts(
                            query=block.input.get("query", ""),
                            limit=5,
                        )
                        if not results:
                            answer = "No matching contacts found in HubSpot."
                        else:
                            lines = []
                            for c in results:
                                line = f"• {c['name']} (ID: {c['id']})"
                                if c["email"]: line += f" — {c['email']}"
                                if c["phone"]: line += f" — {c['phone']}"
                                if c["status"]: line += f" — Status: {c['status']}"
                                lines.append(line)
                            answer = "HubSpot contacts found:\n" + "\n".join(lines)
                    except Exception as e:
                        answer = f"HubSpot search error: {e}"
            elif block.name == "update_hubspot_contact":
                delegated_to.append("HubSpot CRM")
                if not os.environ.get("HUBSPOT_ACCESS_TOKEN"):
                    answer = "HubSpot isn't connected yet. Set HUBSPOT_ACCESS_TOKEN in Render environment variables. See HUBSPOT_SETUP_GUIDE.md for full instructions."
                else:
                    try:
                        contact_id = block.input.get("contact_id", "")
                        props = {k: v for k, v in block.input.items()
                                 if k != "contact_id" and v}
                        hubspot.update_contact(contact_id, **props)
                        answer = f"HubSpot contact {contact_id} updated."
                    except Exception as e:
                        answer = f"HubSpot update error: {e}"
            elif block.name == "update_hubspot_deal_stage":
                delegated_to.append("HubSpot CRM")
                if not os.environ.get("HUBSPOT_ACCESS_TOKEN"):
                    answer = "HubSpot isn't connected yet. Set HUBSPOT_ACCESS_TOKEN in Render environment variables. See HUBSPOT_SETUP_GUIDE.md for full instructions."
                else:
                    try:
                        hubspot.update_deal_stage(
                            deal_id=block.input.get("deal_id", ""),
                            stage=block.input.get("stage", ""),
                        )
                        answer = f"Deal {block.input.get('deal_id')} moved to stage '{block.input.get('stage')}'."
                    except Exception as e:
                        answer = f"HubSpot deal update error: {e}"
            elif block.name == "get_hubspot_deals":
                delegated_to.append("HubSpot CRM")
                if not os.environ.get("HUBSPOT_ACCESS_TOKEN"):
                    answer = "HubSpot isn't connected yet. Set HUBSPOT_ACCESS_TOKEN in Render environment variables. See HUBSPOT_SETUP_GUIDE.md for full instructions."
                else:
                    try:
                        deals = hubspot.get_deals(
                            limit=block.input.get("limit", 10),
                            stage=block.input.get("stage", ""),
                        )
                        if not deals:
                            answer = "No deals found in HubSpot."
                        else:
                            lines = []
                            for d in deals:
                                line = f"• {d['name']} (ID: {d['id']}) — Stage: {d['stage']}"
                                if d["amount"]: line += f" — ${d['amount']}"
                                lines.append(line)
                            answer = "HubSpot deals:\n" + "\n".join(lines)
                    except Exception as e:
                        answer = f"HubSpot deals error: {e}"
            elif block.name == "get_buildertrend_jobs":
                delegated_to.append("Buildertrend")
                if not os.environ.get("BUILDERTREND_ACCESS_TOKEN"):
                    answer = "Buildertrend isn't connected yet — add BUILDERTREND_ACCESS_TOKEN to the Render environment."
                else:
                    try:
                        job_id = block.input.get("job_id")
                        if job_id:
                            job = buildertrend.get_job(job_id)
                            milestones = buildertrend.list_milestones(job_id)
                            answer = f"Job: {job}\n\nMilestones: {milestones}"
                        else:
                            jobs = buildertrend.get_jobs(block.input.get("status", "Active"))
                            answer = f"Active jobs ({len(jobs)}): " + ", ".join(
                                f"{j.get('name', j.get('id', '?'))}" for j in jobs[:10]
                            )
                    except Exception as e:
                        answer = f"Buildertrend error: {e}"
            elif block.name == "send_buildertrend_message":
                delegated_to.append("Buildertrend (message sent)")
                if not os.environ.get("BUILDERTREND_ACCESS_TOKEN"):
                    answer = "Buildertrend isn't connected yet."
                else:
                    try:
                        result = buildertrend.send_message(
                            block.input.get("job_id", ""),
                            block.input.get("subject", ""),
                            block.input.get("body", ""),
                        )
                        answer = f"Message sent via Buildertrend client portal. Result: {result}"
                    except Exception as e:
                        answer = f"Buildertrend message error: {e}"
            elif block.name == "create_lightspeed_invoice":
                delegated_to.append("Lightspeed Billing")
                if not lightspeed.is_configured():
                    answer = "Lightspeed isn't connected yet — add LIGHTSPEED_ACCESS_TOKEN and LIGHTSPEED_BUSINESS_ID to Render."
                else:
                    try:
                        result = lightspeed.bill_client(
                            name=block.input.get("name", ""),
                            email=block.input.get("email", ""),
                            phone=block.input.get("phone", ""),
                            line_items=block.input.get("items", []),
                            note=block.input.get("note", ""),
                        )
                        cust_id = result["customer"].get("customerID") or result["customer"].get("id", "?")
                        sale_id = result["sale"].get("saleID") or result["sale"].get("id", "?")
                        answer = f"Invoice created in Lightspeed. Customer ID: {cust_id}, Sale ID: {sale_id}."
                    except Exception as e:
                        answer = f"Lightspeed error: {e}"
            elif block.name == "list_lightspeed_invoices":
                delegated_to.append("Lightspeed Billing")
                if not lightspeed.is_configured():
                    answer = "Lightspeed isn't connected yet."
                else:
                    try:
                        invoices = lightspeed.list_invoices(
                            customer_id=block.input.get("customer_id", ""),
                            limit=block.input.get("limit", 20),
                        )
                        if not invoices:
                            answer = "No invoices found."
                        else:
                            lines = []
                            for inv in invoices[:10]:
                                sid = inv.get("saleID") or inv.get("id", "?")
                                total = inv.get("total", "?")
                                status = inv.get("completed", "?")
                                lines.append(f"Sale {sid} — ${total} — completed: {status}")
                            answer = f"{len(invoices)} invoice(s):\n" + "\n".join(lines)
                    except Exception as e:
                        answer = f"Lightspeed error: {e}"
            elif block.name == "record_lightspeed_payment":
                delegated_to.append("Lightspeed Billing")
                if not lightspeed.is_configured():
                    answer = "Lightspeed isn't connected yet."
                else:
                    try:
                        result = lightspeed.create_payment(
                            sale_id=block.input.get("sale_id", ""),
                            amount=block.input.get("amount", 0),
                            method=block.input.get("method", "Credit Card"),
                            notes=block.input.get("notes", ""),
                        )
                        pid = result.get("salePaymentID") or result.get("id", "?")
                        answer = f"Payment recorded in Lightspeed. Payment ID: {pid}."
                    except Exception as e:
                        answer = f"Lightspeed payment error: {e}"
            elif block.name == "create_stripe_payment_link":
                delegated_to.append("Stripe")
                if not stripe_billing.is_configured():
                    answer = "Stripe isn't connected yet — add STRIPE_SECRET_KEY to Render to activate online payment links."
                else:
                    try:
                        result = stripe_billing.create_payment_link(
                            customer_name=block.input.get("customer_name", ""),
                            customer_email=block.input.get("customer_email", ""),
                            customer_phone=block.input.get("customer_phone", ""),
                            line_items=block.input.get("line_items", []),
                        )
                        answer = f"Stripe payment link created: {result['url']} — share this link with the client and they can pay by card instantly."
                    except Exception as e:
                        answer = f"Stripe error creating payment link: {e}"
            elif block.name == "create_stripe_invoice":
                delegated_to.append("Stripe")
                if not stripe_billing.is_configured():
                    answer = "Stripe isn't connected yet — add STRIPE_SECRET_KEY to Render to activate Stripe invoicing."
                else:
                    try:
                        invoice = stripe_billing.create_invoice(
                            customer_name=block.input.get("customer_name", ""),
                            customer_email=block.input.get("customer_email", ""),
                            customer_phone=block.input.get("customer_phone", ""),
                            line_items=block.input.get("line_items", []),
                            due_days=block.input.get("due_days", 7),
                            memo=block.input.get("memo", ""),
                            auto_send=True,
                        )
                        url = invoice.get("hosted_invoice_url", "")
                        inv_id = invoice.get("id", "?")
                        answer = f"Stripe invoice {inv_id} created and emailed to the client. They can view and pay it here: {url}"
                    except Exception as e:
                        answer = f"Stripe error creating invoice: {e}"
            elif block.name == "list_stripe_invoices":
                delegated_to.append("Stripe")
                if not stripe_billing.is_configured():
                    answer = "Stripe isn't connected yet — add STRIPE_SECRET_KEY to Render."
                else:
                    try:
                        invoices = stripe_billing.list_invoices(
                            customer_email=block.input.get("customer_email", ""),
                            limit=block.input.get("limit", 20),
                        )
                        if not invoices:
                            answer = "No Stripe invoices found."
                        else:
                            lines = []
                            for inv in invoices[:10]:
                                amt = inv.get("amount_due", 0) / 100
                                status = inv.get("status", "unknown")
                                cust = inv.get("customer_email") or inv.get("customer_name") or "unknown"
                                lines.append(f"${amt:.2f} — {status} — {cust} — {inv.get('id','')}")
                            answer = "Recent Stripe invoices:\n" + "\n".join(lines)
                    except Exception as e:
                        answer = f"Stripe error listing invoices: {e}"
            elif block.name == "send_proposal_docusign":
                delegated_to.append("DocuSign")
                if not os.environ.get("DOCUSIGN_ACCESS_TOKEN"):
                    answer = "DocuSign isn't connected yet — add DOCUSIGN_ACCESS_TOKEN, DOCUSIGN_ACCOUNT_ID, and DOCUSIGN_BASE_URL to Render."
                else:
                    try:
                        from . import docusign_helper as ds
                        env_id = ds.send_proposal_for_signature(
                            signer_name=block.input.get("signer_name", ""),
                            signer_email=block.input.get("signer_email", ""),
                            signer_phone=block.input.get("signer_phone", ""),
                            document_html=block.input.get("document_html", ""),
                            document_name=block.input.get("document_name", "Proposal"),
                            email_subject=block.input.get("email_subject", "Your proposal is ready to sign"),
                        )
                        answer = f"DocuSign envelope sent. Envelope ID: {env_id}. The client will receive an email to sign."
                    except Exception as e:
                        answer = f"DocuSign error: {e}"
            elif block.name == "list_dropbox_folder":
                delegated_to.append("Dropbox")
                try:
                    entries = files_dropbox.list_folder(block.input.get("path", ""))
                    answer = json.dumps(entries, default=str) if entries else "Folder is empty."
                except Exception as e:
                    answer = f"Dropbox error: {e}"
            elif block.name == "search_dropbox":
                delegated_to.append("Dropbox")
                try:
                    hits = files_dropbox.search(block.input.get("query", ""))
                    answer = json.dumps(hits, default=str) if hits else "No Dropbox matches."
                except Exception as e:
                    answer = f"Dropbox search error: {e}"
            elif block.name == "save_dropbox_file":
                delegated_to.append("Dropbox → Asset Library")
                try:
                    answer = files_dropbox.save_to_asset_library(
                        block.input.get("path", ""),
                        block.input.get("name", ""),
                        block.input.get("tags", ""),
                    )
                except Exception as e:
                    answer = f"Dropbox save error: {e}"
            elif block.name == "list_drive_files":
                delegated_to.append("Google Drive")
                try:
                    files = files_gdrive.list_files(block.input.get("folder_id", ""))
                    answer = json.dumps(files, default=str) if files else "No files."
                except Exception as e:
                    answer = f"Drive error: {e}"
            elif block.name == "search_drive":
                delegated_to.append("Google Drive")
                try:
                    hits = files_gdrive.search(block.input.get("query", ""))
                    answer = json.dumps(hits, default=str) if hits else "No Drive matches."
                except Exception as e:
                    answer = f"Drive search error: {e}"
            elif block.name == "save_drive_file":
                delegated_to.append("Google Drive → Asset Library")
                try:
                    answer = files_gdrive.save_to_asset_library(
                        block.input.get("file_id", ""),
                        block.input.get("name", ""),
                        block.input.get("tags", ""),
                    )
                except Exception as e:
                    answer = f"Drive save error: {e}"
            elif block.name == "run_diagnostic":
                delegated_to.append("Diagnostic")
                # Guarded like its neighbours (drive save, etc.): a crash inside
                # a tool must degrade to a tool-result string, NEVER escape and
                # 500 the whole chat. It did exactly that here -- run_all()
                # referenced an undefined PROBES -- so asking "is everything
                # working?" took the entire assistant down instead of answering.
                try:
                    report = diagnostic.run_all()
                    lines = [f"System status: {report['summary'].upper()} "
                             f"({report['counts']['green']} ok, "
                             f"{report['counts']['red']} failing, "
                             f"{report['counts']['unconfigured']} unconfigured, "
                             f"{report['counts']['total']} total)."]
                    for s in report["services"]:
                        if s.get("ok") is False:
                            lines.append(f"  FAIL {s['name']}: {s.get('error') or 'probe failed'} — {s.get('hint') or ''}")
                        elif s.get("ok") is True:
                            lat = s.get("latency_ms")
                            lines.append(f"  OK   {s['name']}" + (f" ({lat} ms)" if lat is not None else ""))
                        elif not s.get("configured"):
                            lines.append(f"  --   {s['name']}: not configured — {s.get('hint') or ''}")
                        else:
                            lines.append(f"  ?    {s['name']}: configured, not actively probed")
                    lines.append("Full board: /diagnostic")
                    answer = "\n".join(lines)
                except Exception as e:
                    answer = f"Couldn't run the system diagnostic ({type(e).__name__}: {e})."
            elif block.name == "ask_seo_auditor":
                delegated_to.append("SEO Auditor")
                audit_text = block.input.get("audit_results", "")
                context = block.input.get("prospect_context", "")
                brief = f"Audit results:\n{audit_text}\n\nContext: {context}"
                answer = call_sub_agent("seo_auditor", brief)
            elif block.name == "search_gis_parcel":
                delegated_to.append("GIS Lookup")
                from . import gis
                address = block.input.get("address", "")
                county = block.input.get("county", "")
                result = gis.lookup_parcel(address, county)
                answer = str(result)
            elif block.name == "ask_pricing_advisor":
                # Deterministic lookup, not a model call -- the figure returned
                # is always exactly what's in pricing_data.PRICING, so it can't
                # be rounded, transposed, or invented in transit.
                delegated_to.append("Pricing Advisor")
                pricing_query = block.input.get("query", "")
                answer = pricing_data.lookup(pricing_query)
                crm.log_verification(pricing_query, "Ground Truth", source="pricing_data.py catalog", detail=answer)
            elif block.name == "flag_for_review":
                delegated_to.append("Escalated")
                flag_question = block.input.get("question", "")
                flag_reason = block.input.get("reason", "")
                crm.log_verification(flag_question, "Escalated", source="Annabelle self-flagged", detail=flag_reason)
                answer = "Flagged for the team to review and follow up on -- no answer given until it's confirmed."
            elif agent_key is None:
                answer = unbuilt.refuse(block.name)
            else:
                delegated_to.append(SUB_AGENTS[agent_key]["name"])
                # Read every argument name the delegation tools actually
                # declare: ask_bear_arms_agent / ask_peptides_agent declare
                # "question", and reading only query/briefing silently threw
                # away the model's rephrased question and forwarded the raw
                # user message instead.
                query = (block.input.get("query")
                         or block.input.get("question")
                         or block.input.get("briefing", user_message))
                answer = call_sub_agent(agent_key, query, active_model)
                if answer.strip().startswith("NEEDS_SEARCH:"):
                    search_query = answer.split("NEEDS_SEARCH:", 1)[1].strip()
                    if search_available and crm.get_search_count() < get_search_cap():
                        search_text, used = run_web_search(search_query, active_model)
                        if used:
                            crm.increment_search_count(used)
                            delegated_to.append("Web Search")
                        # Tier 2 discipline: a search that came back empty (or
                        # near-empty) gives the sub-agent nothing real to ground
                        # an answer in -- re-asking it anyway risks it filling
                        # the gap with something plausible-sounding instead of
                        # honestly saying it doesn't know. Escalate instead.
                        if len((search_text or "").strip()) < 40:
                            answer = (
                                "I don't have reliable, current information to answer that "
                                "accurately -- I've flagged it for Vinny to follow up on "
                                "rather than guess."
                            )
                            crm.log_verification(search_query, "Escalated", source="Web search returned nothing usable")
                        else:
                            from datetime import date as _date
                            search_context = (
                                f"[WEB SEARCH RESULT — retrieved {_date.today()} — "
                                "may be outdated or inaccurate. Cite the source when "
                                "presenting this information to the user.]\n\n"
                                + search_text
                            )
                            answer = call_sub_agent(
                                agent_key,
                                f"{query}\n\nHere is current web search information you can use:\n{search_context}",
                                active_model,
                            )
                            crm.log_verification(search_query, "Verified", source="Web search", detail=search_text[:1500])
                    else:
                        answer = (
                            "I don't have live search access for that right now (the search "
                            "budget is capped for this period) -- Vinny, you'll need to raise "
                            "the cap or check back next cycle."
                        )
                        crm.log_verification(search_query, "Escalated", source="Search budget capped")
                # Auto quality-review: proposals and audits pass through the reviewer
                # before reaching Vinny. If reviewer returns REVISED:, use the improved doc.
                if agent_key in ("proposal_writer", "audit_writer") and not answer.strip().startswith("NEEDS_SEARCH:"):
                    try:
                        delegated_to.append("Proposal Quality Reviewer")
                        review = call_sub_agent("proposal_reviewer", answer, active_model)
                        if review.strip().startswith("REVISED:"):
                            answer = review.strip()[len("REVISED:"):].strip()
                    except Exception as e:
                        log.warning("Proposal reviewer failed: %s", e)
                        answer = (
                            "⚠ REVIEW SKIPPED — The automatic accuracy check "
                            "failed due to a system error. Verify this proposal "
                            "manually before sending to the client.\n\n"
                            + answer
                        )
                # Long-form documents get a durable, shareable link -- the
                # Artifacts panel is otherwise permanently empty since these
                # tools only ever returned inline text before.
                if agent_key in ("proposal_writer", "audit_writer", "content_writer") and not answer.strip().startswith("NEEDS_SEARCH:"):
                    doc_title = (
                        block.input.get("prospect_name") or block.input.get("company")
                        or block.input.get("content_type") or block.input.get("client_name")
                        or {"proposal_writer": "Proposal", "audit_writer": "Opportunity Audit",
                            "content_writer": "Content"}[agent_key]
                    )
                    slug = crm.create_artifact(doc_title, answer)
                    if slug:
                        artifact_url = f"/artifact/{slug}"
                        artifact_title = doc_title
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
    # Ran out of tool rounds. If she narrated anything usable along the way,
    # that's a better answer than the generic apology.
    stuck_text = "".join(spoken_so_far).strip() or (
        "Sorry, I got stuck coordinating that -- try rephrasing your question."
    )
    yield {"type": "done", "response": ChatResponse(
        reply=stuck_text,
        delegated_to=delegated_to,
        timings=timings,
        artifact_url=artifact_url,
        artifact_title=artifact_title,
        speaker_name=speaker_name,
    )}


def run_main_brain(user_message: str, history: List[Dict[str, str]],
                   system_prompt: str = MAIN_BRAIN_SYSTEM_PROMPT,
                   tools=DELEGATION_TOOLS, enable_search: bool = False,
                   persona: str = "owner", file: Optional["FileAttachment"] = None,
                   request_id: str = "", model: str = "", chat_id: str = "",
                   business: str = "") -> ChatResponse:
    """Blocking form: drain the event stream, hand back the final response.
    Kept so /api/public-chat and the widget keep working exactly as before."""
    final: Optional[ChatResponse] = None
    for ev in _run_main_brain_events(user_message, history, system_prompt, tools,
                                     enable_search, persona, file, request_id, model, chat_id,
                                     business):
        if ev["type"] == "done":
            final = ev["response"]
    if final is None:  # generator exhausted without a done event -- shouldn't happen
        final = ChatResponse(reply="Sorry, something went wrong composing that reply.", delegated_to=[])
    return final


class StopChatRequest(BaseModel):
    request_id: str = ""


@app.post("/api/chat/stop")
def stop_chat(req: StopChatRequest) -> JSONResponse:
    """Client-side Stop button. Marks a request_id cancelled so run_main_brain
    bails before its next Anthropic call or tool execution instead of running
    the full (possibly slow) turn to completion. Not gated -- worst case
    someone cancels a request_id that isn't theirs, which does nothing harmful."""
    _mark_cancelled(req.request_id[:100])
    return JSONResponse({"ok": True})


def _get_learning_injection() -> str:
    """Return a compact learning note from the latest test run report."""
    try:
        from .test_learning_system import TestLearningSystem
        tls = TestLearningSystem()
        report = tls.get_learning_report()
        if not report:
            return ""
        # Summarize for the system prompt — compact, actionable
        parts = []
        if report.performance_summary:
            parts.append(f"Test performance: {report.performance_summary}")
        if report.improvement_areas:
            areas = "; ".join(report.improvement_areas[:3])
            parts.append(f"Current improvement focus: {areas}.")
        if report.failure_patterns:
            top = report.failure_patterns[0]
            parts.append(f"Most common failure pattern: {top.get('description', 'unknown')} — address proactively.")
        if not parts:
            return ""
        note = "\n\n[LEARNING UPDATE] " + " ".join(parts)
        return note
    except Exception:
        return ""


def _get_update_injection() -> str:
    """Return a system-level update note if Annabelle has new improvements to announce."""
    import json as _json
    updates_path = os.path.join(os.path.dirname(__file__), "annabelle_updates.json")
    try:
        with open(updates_path, "r", encoding="utf-8") as f:
            data = _json.load(f)
        current_version = data.get("version", "")
        announced = data.get("announced_version", "")
        if current_version and current_version != announced:
            all_updates = data.get("updates", [])
            total_updates = sum(len(u.get("changes", [])) for u in all_updates)
            latest = all_updates[0] if all_updates else {}
            changes = latest.get("changes", [])
            changes_list = "\n".join(f"  - {c}" for c in changes)
            total_versions = len(all_updates)
            note = (
                f"\n\n[SYSTEM UPDATE — v{current_version} — {latest.get('date', 'today')}]\n"
                f"You have been updated and improved. This is improvement version {current_version}. "
                f"Across {total_versions} update sessions, you have received {total_updates} individual improvements. "
                f"You are getting smarter and more reliable with every session.\n"
                f"Latest changes:\n{changes_list}\n"
                f"You may mention this update naturally to Vinny once at the start of this session "
                f"(e.g. 'I got some updates since we last talked') — but do not repeat it every turn. "
                f"Do not say 'SYSTEM UPDATE' verbatim — just mention it conversationally."
            )
            # Mark as announced (best-effort, non-blocking)
            try:
                data["announced_version"] = current_version
                with open(updates_path, "w", encoding="utf-8") as f:
                    _json.dump(data, f, indent=2)
            except Exception:
                pass
            return note
    except Exception:
        pass
    return ""


def _normalize_url(u: str) -> str:
    """Tidy a hand-typed website: trim and prepend https:// when no scheme is
    present, so "thedreamerie.com" becomes a real clickable URL. Grooming, not
    validation -- we reject nothing."""
    u = (u or "").strip()
    if u and not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    return u


def _business_websites() -> dict:
    """{brand_key: url} for each business that has a website saved."""
    if not crm.is_configured():
        return {}
    out = {}
    for b in brand_identity.BRANDS:
        v = crm.get_setting(f"website__{b}", "")
        if v:
            out[b] = v
    return out


def _website_context(mode: str) -> str:
    """A prompt snippet naming the business website(s) so Annabelle shares the
    real URL instead of inventing one. Scoped to the active business; in
    combined mode she is told all of them."""
    sites = _business_websites()
    if not sites:
        return ""
    labels = brand_identity.BRANDS
    # A specific business is active: give her ONLY that business's site, never
    # another brand's -- handing her the Dreamerie URL while she's focused on
    # Bear Arms is exactly the cross-brand leak the whole mode system prevents.
    # No site for the active business => say nothing.
    if mode in labels:
        if mode in sites:
            return ("\n\nThe " + labels[mode] + " website is " + sites[mode]
                    + " -- share this exact URL when asked where to find or buy from "
                    + labels[mode] + ", and use it in " + labels[mode]
                    + " content. Never invent or alter a URL.")
        return ""
    # Combined (or no specific business): she may reference any of them.
    listed = "; ".join(labels[b] + ": " + url for b, url in sites.items())
    return "\n\nBusiness websites (use the exact URL, never invent one): " + listed + "."


def _now_context() -> str:
    """Current date & time, injected fresh into EVERY turn's system prompt.

    Without this the model has no clock at all: it dated follow-ups from its
    training data and agreed with whatever "today" the user implied. Anything
    time-sensitive -- deadlines, events, "post this tomorrow" -- needs the model
    to know when NOW is. Eastern because Susan (Queens) and Vinny both live
    there; falls back to UTC (labelled, never silently) if tzdata is missing.
    """
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
        tz_label = "Eastern"
    except Exception:
        now = datetime.now(timezone.utc)
        tz_label = "UTC"
    stamp = now.strftime("%A, %B %d, %Y at %I:%M %p").replace(" 0", " ")
    return ("\n\nCURRENT DATE & TIME: " + stamp + " " + tz_label + ". "
            "This is authoritative -- trust it over your training data and over "
            "dates mentioned earlier in the conversation. Anchor every deadline, "
            "schedule, 'today'/'tomorrow'/'this week', and follow-up you state to "
            "it, and when a task is date-sensitive, say the date you're working "
            "from so it can be corrected if the intent was different.")


def _owner_chat_setup(req: ChatRequest):
    """System prompt + tool set for an owner turn, derived from the active mode.
    Shared by /api/chat and /api/chat/stream so the two can never drift."""
    update_note = (_get_update_injection() + _get_learning_injection()) if not req.history else ""
    sys_prompt = build_main_brain_prompt(_current_agent_name()) + get_automation_level_prompt(get_automation_level()) + update_note + _now_context()
    tools = config_check.filter_tools(DELEGATION_TOOLS)
    if req.mode in MODE_TOOLS:
        tools = [t for t in tools if t["name"] in MODE_TOOLS[req.mode]]
        sys_prompt += MODE_PROMPTS[req.mode]
    sys_prompt += _website_context(req.mode)
    return sys_prompt, tools


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest, request: Request) -> ChatResponse:
    req.clean()
    sys_prompt, tools = _owner_chat_setup(req)
    try:
        result = run_main_brain(req.message, req.history, sys_prompt, tools, enable_search=True,
                                 persona="owner", file=req.file, request_id=req.request_id,
                                 model=req.model, chat_id=_scoped_chat_id(request, req.chat_id),
                                 business=req.mode)
    finally:
        _clear_cancelled(req.request_id)
    if result.reply == STOPPED_REPLY:
        return result
    # Persist the exchange to durable memory (Airtable) in the background --
    # measured at ~1.3s on production, and the user shouldn't wait on
    # bookkeeping after the reply is already composed.
    _ts = time.perf_counter()
    _persist_turn(request, req, result)
    result.timings["save"] = round(time.perf_counter() - _ts, 3)
    return result


def _persist_turn(request: Request, req: ChatRequest, result: ChatResponse) -> None:
    """Background-save the exchange and sync the active speaker back to the
    client. Same bookkeeping /api/chat has always done, lifted out so the
    streaming endpoint does it identically."""
    def _persist(user_msg: str, reply: str, chat_id: str, speaker: str) -> None:
        crm.save_turn("user", user_msg, chat_id, speaker)
        crm.save_turn("assistant", reply, chat_id, speaker)

    scoped_chat_id = _scoped_chat_id(request, req.chat_id)
    effective_speaker = result.speaker_name if result.speaker_name is not None else req.speaker
    result.speaker_name = effective_speaker
    threading.Thread(target=_persist,
                     args=(req.message, result.reply, scoped_chat_id, effective_speaker),
                     daemon=True).start()


@app.post("/api/chat/stream")
def chat_stream(req: ChatRequest, request: Request) -> StreamingResponse:
    """Server-Sent Events form of /api/chat.

    Same inputs, same work, same bookkeeping -- the difference is that text
    reaches the browser token-by-token, so the client can start speaking the
    first sentence while the rest is still being written. /api/chat stays as
    it is for the public widget and as a fallback if this ever fails.

    Event stream (one JSON object per `data:` line):
      {"type":"text","text":...}   append to the bubble; feed the speech queue
      {"type":"tool","name":...}   a sub-agent fired, for the live log
      {"type":"done","response":{...ChatResponse...}}
      {"type":"error","message":...}
    """
    req.clean()
    sys_prompt, tools = _owner_chat_setup(req)

    def event_source():
        result: Optional[ChatResponse] = None
        try:
            for ev in _run_main_brain_events(req.message, req.history, sys_prompt, tools,
                                             enable_search=True, persona="owner", file=req.file,
                                             request_id=req.request_id, model=req.model,
                                             chat_id=_scoped_chat_id(request, req.chat_id),
                                             business=req.mode):
                if ev["type"] == "done":
                    result = ev["response"]
                    if result.reply != STOPPED_REPLY:
                        _ts = time.perf_counter()
                        _persist_turn(request, req, result)
                        result.timings["save"] = round(time.perf_counter() - _ts, 3)
                    yield "data: " + json.dumps({"type": "done", "response": result.model_dump()}) + "\n\n"
                else:
                    yield "data: " + json.dumps(ev) + "\n\n"
        except Exception as e:
            logging.exception("chat_stream failed")
            yield "data: " + json.dumps({"type": "error", "message": f"{type(e).__name__}: {e}"}) + "\n\n"
        finally:
            _clear_cancelled(req.request_id)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # stop any proxy from buffering the stream flat
            "Connection": "keep-alive",
        },
    )


@app.get("/api/history")
def history(request: Request, limit: int = 40, chat_id: str = "default") -> JSONResponse:
    """Return recent conversation turns from durable memory (oldest first),
    scoped to whoever is logged in so different accounts never see each
    other's conversation.

    If we can't identify the account, say so rather than returning an empty
    list -- an empty list is indistinguishable from "you have no history" and
    is what made the memory-loss bug invisible."""
    scoped = _scoped_chat_id_checked(request, chat_id)
    if scoped is None:
        return JSONResponse(
            {"history": [], "authed": False,
             "detail": "Session expired -- sign in again to load this conversation."},
            status_code=401)
    return JSONResponse({"history": crm.get_history(limit, scoped), "authed": True})


@app.get("/api/memory/health")
def memory_health(request: Request, chat_id: str = "default") -> JSONResponse:
    """Live snapshot of memory persistence: is Airtable reachable, how many
    turns are stored for this chat_id, when was the last successful save/load,
    and if something failed, WHY. Owner-only (behind the access gate)."""
    chat_id = _scoped_chat_id(request, chat_id)
    stats = crm.memory_stats()
    turns = crm.get_history(200, chat_id)  # actual round-trip verifies reachability
    stats["airtable_configured"] = crm.is_configured()
    stats["airtable_reachable"] = stats["last_load_ok_ts"] is not None and (
        stats["last_load_err_ts"] is None
        or stats["last_load_ok_ts"] > stats["last_load_err_ts"]
    )
    stats["chat_id"] = chat_id
    stats["turn_count"] = len(turns)
    return JSONResponse(stats)


@app.get("/healthz")
def healthz() -> JSONResponse:
    """Tiny uptime probe. Public, no auth, no side effects. Returns 200 as
    long as the FastAPI worker is alive. UptimeRobot / any external monitor
    should hit this every 5 minutes.

    Also reports which revision is actually serving. Every app page sits behind
    the access gate, so without this there is no ungated way to tell a deployed
    fix from an unshipped one -- and "pushed" is not "deployed". Render sets
    RENDER_GIT_COMMIT on each deploy; locally it is absent and reports
    "unknown", which is itself the correct answer."""
    return JSONResponse({
        "ok": True,
        "ts": datetime.now(timezone.utc).isoformat(),
        "rev": (os.environ.get("RENDER_GIT_COMMIT") or "unknown")[:12],
    })


class InboxItem(BaseModel):
    title: str = ""
    body: str = ""
    kind: str = "update"
    url: str = ""
    sender: str = "Stinger Industries"


@app.post("/api/inbox")
def receive_inbox_item(item: InboxItem, request: Request) -> JSONResponse:
    """Receive something Stinger pushed into THIS deployment's assistant.

    Authenticated by a shared secret (X-Inbox-Secret), NOT the access code -- a
    machine-to-machine push shouldn't need a human credential, and rotating the
    access code shouldn't silently break the feed. Exempt from the browser gate
    but enforces its own secret; a deployment that never set CLIENT_INBOX_SECRET
    refuses everything rather than defaulting open.

    Items land tagged with WHO sent them: an assistant quoting research it can't
    attribute is the fabrication problem wearing a different hat.
    """
    if not inbox.is_receiving():
        return JSONResponse(
            {"ok": False, "detail": "This deployment doesn't accept pushes "
                                    "(CLIENT_INBOX_SECRET isn't set)."},
            status_code=403)
    if not inbox.verify_secret(request.headers.get("X-Inbox-Secret", "")):
        log.warning("INBOX_REJECTED wrong or missing shared secret")
        return JSONResponse({"ok": False, "detail": "Bad secret."}, status_code=401)
    ok, msg = inbox.store(item.title, item.body, item.kind, item.url, item.sender)
    if not ok:
        log.error("INBOX_SAVE_FAIL %s", msg)
        return JSONResponse({"ok": False, "detail": msg}, status_code=502)
    log.info("INBOX_RECEIVED title=%s from=%s", item.title, item.sender)
    return JSONResponse({"ok": True, "detail": msg})


@app.post("/api/chats/create")
def create_chat(request: Request, name: str = "New Chat") -> JSONResponse:
    """Create a new named chat session and return the chat_id, scoped to
    whoever is logged in."""
    owner = _authed_username(request) or "shared"
    chat_id = crm.create_chat_session(name, owner)
    return JSONResponse({"chat_id": chat_id, "name": name})


@app.get("/api/chats/list")
def list_chats(request: Request) -> JSONResponse:
    """Return chat sessions belonging to whoever is logged in, newest first."""
    owner = _authed_username(request) or "shared"
    return JSONResponse({"chats": crm.get_chat_sessions(owner)})


@app.get("/api/toolbox")
def get_toolbox() -> JSONResponse:
    """Skill Toolbox cards for the dashboard -- every owner-mode capability
    with a friendly title, group, and example trigger phrases. Generated from
    DELEGATION_TOOLS so new skills self-register."""
    return JSONResponse({"cards": toolbox.get_cards()})


# ── Content Creation panel ───────────────────────────────────────────────────
# social.py has had drafting, the review queue and Zapier publishing since day
# one, and media_gen.py has had image/video generation -- but all of it was only
# reachable as Annabelle's chat tools. There were no HTTP endpoints, so the
# dashboard couldn't offer any of it. These wrap the existing functions; the
# behaviour (caps, media-required platforms, draft-then-approve) is unchanged.

# This prompt was inherited from the flagship and still described the VENDOR'S
# businesses (bee removal and Stinger Industries) -- so asking Susan's app for
# a social post produced copy for somebody else's company. Same bug class as
# Tidemark's, fixed the same day. The four businesses below are this
# platform's own, matching _PLATFORM_SCOPE in agents.py; the compliance lines
# for Bear Arms and NS Peptides are load-bearing, not tone advice.
_CONTENT_WRITER_SYSTEM = """You write social media copy for the four real
businesses on this platform. Every post is for ONE of them -- pick from the
topic, never blend brands in one post.

The Dreamerie -- Susan's shop: products, markets, events and the customers who
come back. Warm, local, handmade-feel.
Suzy D -- TikTok and social growth: content, captions, cadence, live strategy.
Bear Arms -- Nick's firearms-accessory e-commerce (dropship, NYC). STRICT
compliance: accessories and preparedness gear only; never write copy that
promotes a firearm itself, ammunition, or anything the major ad platforms ban;
no gun-to-person imagery suggestions.
NS Peptides -- Nick's peptide venture. RESEARCH USE ONLY: no health, dosing,
healing, weight-loss or human-use claims of any kind, no before/after framing,
and NEVER the phrase "discreet shipping". If the requested topic requires a
claim these rules forbid, refuse the topic in one plain sentence instead of
softening the claim.

Rules:
- Write in a warm, plain, human voice. No corporate filler, no hype stacking,
  no emoji walls (one or two is fine where it genuinely helps).
- NEVER invent statistics, review counts, years in business, customer numbers,
  prices, or awards. If a number would strengthen the post, leave a clearly
  marked [VERIFY: ...] placeholder instead of guessing. Made-up numbers about a
  real business are the single worst failure here.
- No claims about being licensed, insured, certified, or #1 unless the topic
  the owner gave you explicitly says so.

Reply in EXACTLY this format, nothing before or after:
CONTENT:
<the post body>
HASHTAGS:
<space-separated hashtags, or leave blank if they don't suit the platform>"""

_PLATFORM_SHAPE = {
    "X": "one tight post under 280 characters",
    "Facebook": "2-3 short paragraphs, conversational",
    "Instagram": "2-3 short paragraphs, no clickable links (they don't work in captions)",
    "TikTok": "a spoken hook in the first line, then 3-5 short beats for the voiceover",
    "YouTube": "a title line, then a 3-4 sentence description",
}


class ContentWriteIn(BaseModel):
    platform: str = ""
    topic: str = ""
    tone: str = ""


class ContentDraftIn(BaseModel):
    platform: str = ""
    content: str = ""
    title: str = ""
    hashtags: str = ""
    media_url: str = ""


class ContentPublishIn(BaseModel):
    post_id: str = ""


class ContentMediaIn(BaseModel):
    prompt: str = ""
    aspect_ratio: str = ""
    duration: int = 8
    image_url: str = ""


@app.get("/api/content/overview")
def content_overview(status: str = "", limit: int = 25) -> JSONResponse:
    """Everything the Content panel needs in one call: what's connected, where
    the media spend caps stand, and the current review queue."""
    return JSONResponse({
        "queue_configured": crm.is_configured(),
        "publishing_configured": social.is_configured(),
        "platforms": social.connected_platforms(),
        "all_platforms": social.PLATFORMS,
        "media_required": social.MEDIA_REQUIRED,
        "media_configured": media_gen.is_configured(),
        "image_used": crm.get_media_count("image"),
        "image_cap": get_image_cap(),
        "video_used": crm.get_media_count("video"),
        "video_cap": get_video_cap(),
        "posts": social.list_posts_structured(status, limit),
    })


@app.post("/api/content/write")
def content_write(body: ContentWriteIn) -> JSONResponse:
    """Draft platform-shaped copy. Returns the text plus any numeric claims it
    made, so the owner can verify them before anything is saved."""
    topic = (body.topic or "").strip()
    if not topic:
        return JSONResponse({"ok": False, "error": "Give me a topic or angle to write about."})
    platform = (body.platform or "Facebook").strip()
    tone = (body.tone or "").strip() or "warm and practical"
    shape = _PLATFORM_SHAPE.get(platform, "a short post")
    try:
        resp = client.messages.create(
            model=resolve_model(),
            max_tokens=1200,
            system=_CONTENT_WRITER_SYSTEM,
            messages=[{"role": "user", "content":
                       f"Platform: {platform} -- write {shape}.\n"
                       f"Tone: {tone}\n"
                       f"Topic / angle: {topic}"}],
        )
        raw = "".join(getattr(b, "text", "") for b in resp.content).strip()
        # Tolerant label parsing. The model usually emits a clean "CONTENT:" /
        # "HASHTAGS:" pair, but not always -- it has produced mangled labels
        # ("CONTENTravel:") and markdown-wrapped ones ("**CONTENT:**"). An exact
        # string strip leaves that garbage at the top of the owner's post, so
        # match the label loosely instead: optional bold, CONTENT plus any
        # trailing word characters, optional colon.
        m = re.search(r"\**\s*HASHTAGS\w*\s*:?\s*\**", raw, re.I)
        if m:
            content_txt, hashtags_txt = raw[:m.start()], raw[m.end():].strip()
        else:
            content_txt, hashtags_txt = raw, ""
        content_txt = re.sub(r"^\s*\**\s*CONTENT\w*\s*:?\s*\**\s*", "",
                             content_txt, count=1, flags=re.I).strip()
        return JSONResponse({
            "ok": True,
            "content": content_txt,
            "hashtags": hashtags_txt,
            "claims": social.extract_claims(content_txt),
            "needs_media": social.MEDIA_REQUIRED.get(platform, ""),
        })
    except Exception as e:
        log.exception("content_write failed")
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"})


@app.post("/api/content/draft")
def content_draft(body: ContentDraftIn) -> JSONResponse:
    """Save a Draft row. Never publishes -- same guarantee as the chat tool."""
    result = social.create_draft(
        body.platform or "", body.content or "",
        body.title or "", body.hashtags or "", body.media_url or "",
    )
    ok = result.startswith("DRAFT saved")
    return JSONResponse({"ok": ok, "result": result})


@app.post("/api/content/publish")
def content_publish(body: ContentPublishIn) -> JSONResponse:
    """Send one Draft to its platform's Zapier webhook. Owner-initiated only."""
    post_id = (body.post_id or "").strip()
    if not post_id:
        return JSONResponse({"ok": False, "result": "No post id given."})
    result = social.publish_post(post_id)
    return JSONResponse({"ok": result.startswith("Sent to Zapier"), "result": result})


@app.post("/api/content/image")
def content_image(body: ContentMediaIn) -> JSONResponse:
    """Generate one image and file it in the asset library. Honors the same
    monthly spend cap as the chat tool -- this is a second door to the same
    budget, not a way around it."""
    prompt = (body.prompt or "").strip()
    if not prompt:
        return JSONResponse({"ok": False, "error": "Describe the image you want."})
    if not media_gen.is_configured():
        return JSONResponse({"ok": False, "error": "Image generation isn't connected -- XAI_API_KEY needs to be set."})
    cap = get_image_cap()
    if crm.get_media_count("image") >= cap:
        return JSONResponse({"ok": False, "error": f"Monthly image cap reached ({cap}). Raise it in Settings if you need more."})
    result = media_gen.generate_image(prompt, body.aspect_ratio or "")
    if not result.get("ok"):
        err = result.get("error") or "generation failed"
        return JSONResponse({"ok": False, "error": "Image generation isn't connected yet -- XAI_API_KEY needs to be set." if err == "not_connected" else f"Image generation failed: {err}"})
    crm.increment_media_count("image")
    name = prompt[:60] or "Generated image"
    assets.add_asset(name, result["url"], "Photo", "ai-generated",
                     f"Generated via xAI. Prompt: {prompt[:500]}")
    return JSONResponse({"ok": True, "url": result["url"], "name": name,
                         "used": crm.get_media_count("image"), "cap": cap})


@app.post("/api/content/video")
def content_video(body: ContentMediaIn) -> JSONResponse:
    """Generate one short video and file it in the asset library. Same cap."""
    prompt = (body.prompt or "").strip()
    if not prompt:
        return JSONResponse({"ok": False, "error": "Describe the video you want."})
    if not media_gen.is_configured():
        return JSONResponse({"ok": False, "error": "Video generation isn't connected -- XAI_API_KEY needs to be set."})
    cap = get_video_cap()
    if crm.get_media_count("video") >= cap:
        return JSONResponse({"ok": False, "error": f"Monthly video cap reached ({cap}). Raise it in Settings if you need more."})
    result = media_gen.generate_video(prompt, body.duration or 8,
                                      body.aspect_ratio or "", body.image_url or "")
    if not result.get("ok"):
        err = result.get("error") or "generation failed"
        if err == "not_connected":
            msg = "Video generation isn't connected yet -- XAI_API_KEY needs to be set."
        elif err == "timeout":
            msg = "Still processing -- check the asset library in a minute, it usually lands."
        else:
            msg = f"Video generation failed: {err}"
        return JSONResponse({"ok": False, "error": msg})
    crm.increment_media_count("video")
    name = prompt[:60] or "Generated video"
    assets.add_asset(name, result["url"], "Video", "ai-generated",
                     f"Generated via xAI. Prompt: {prompt[:500]}")
    return JSONResponse({"ok": True, "url": result["url"], "name": name,
                         "duration": result.get("duration", ""),
                         "used": crm.get_media_count("video"), "cap": cap})


@app.get("/api/pending-requests")
def pending_requests() -> JSONResponse:
    """Return build requests still New or Building (owner-only)."""
    all_requests = crm.get_pending_requests()
    pending = [r for r in all_requests if r["status"] in ("New", "Building")]
    return JSONResponse({"pending": pending, "total": len(all_requests)})


@app.post("/api/update-request-status")
def update_request_status_api(req_id: str, status: str) -> JSONResponse:
    """Owner-only. Move a build request between New/Building/Done."""
    return JSONResponse({"ok": crm.update_request_status(req_id, status)})


# ---- Client strategies (Annabelle's sales playbook per prospect) ------------
# Owner-only by virtue of the gate middleware above -- every /api/ path that
# isn't on the exempt list requires a valid cc_session. These let a strategy
# built outside the app be pushed in, and let Annabelle pull it at chat time
# via the get_client_strategy tool.

class StrategyRequest(BaseModel):
    client: str
    content: str
    kind: str = "sales_strategy"
    priority: str = "normal"


@app.post("/api/strategy")
def save_strategy_api(req: StrategyRequest) -> JSONResponse:
    """Owner-only. Store or replace a client strategy. Upserts on
    (client, kind), so re-pushing a revision overwrites rather than
    leaving Annabelle two contradictory versions to choose between."""
    result = crm.save_strategy(
        client=req.client, content=req.content,
        kind=req.kind, priority=req.priority,
    )
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.get("/api/strategies")
def list_strategies_api() -> JSONResponse:
    """Owner-only. Index of stored strategies (no content), high priority first."""
    rows = crm.list_strategies()
    return JSONResponse({"strategies": rows, "total": len(rows)})


@app.get("/api/strategy/{client}")
def get_strategy_api(client: str, kind: str = "") -> JSONResponse:
    """Owner-only. Full stored strategy content for one client."""
    rows = crm.get_strategy(client, kind)
    return JSONResponse({"client": client, "strategies": rows, "total": len(rows)})


# ---- Web Push (phone/lock-screen alerts) -----------------------------------

class PushSubscribeRequest(BaseModel):
    endpoint: str
    p256dh: str
    auth: str


@app.get("/api/push/vapid-public-key")
def push_vapid_public_key() -> JSONResponse:
    """Owner-only. The public half of the VAPID keypair -- safe to hand to
    the browser, it's what PushManager.subscribe() needs. Empty string means
    push isn't configured yet on this deployment."""
    return JSONResponse({"key": push.VAPID_PUBLIC_KEY, "configured": push.is_configured()})


@app.post("/api/push/subscribe")
def push_subscribe(req: PushSubscribeRequest, request: Request) -> JSONResponse:
    """Owner-only. Register this device/browser for lock-screen alert push."""
    ok = crm.add_push_subscription(
        req.endpoint, req.p256dh, req.auth, request.headers.get("user-agent", ""))
    return JSONResponse({"ok": ok})


@app.post("/api/push/unsubscribe")
def push_unsubscribe(req: PushSubscribeRequest) -> JSONResponse:
    """Owner-only. Stop sending push to this device (e.g. notifications toggled off)."""
    crm.remove_push_subscription(req.endpoint)
    return JSONResponse({"ok": True})


@app.post("/api/push/test")
def push_test() -> JSONResponse:
    """Owner-only. Send a one-off test notification to every subscribed device."""
    sent = push.send_to_owner("Test Alert", "Push notifications are working.")
    return JSONResponse({"sent": sent})


@app.post("/run/calendar-reminder")
def run_calendar_reminder(request: Request) -> JSONResponse:
    """Gate-exempt (protected by RUN_SECRET, not a session -- a cron job can't
    hold a login cookie). Triggered by a daily GitHub Actions schedule so the
    owner gets a phone push the day before any calendar event, instead of
    relying on him remembering to open the Schedule panel and look."""
    secret = os.environ.get("RUN_SECRET", "")
    if not secret or request.query_params.get("key") != secret:
        return JSONResponse({"detail": "forbidden"}, status_code=403)
    if not gcal.is_configured():
        return JSONResponse({"sent": 0, "detail": "calendar not connected"})
    if not push.is_configured():
        return JSONResponse({"sent": 0, "detail": "push not connected"})

    result = gcal.list_upcoming_events(days=2)
    # Bucket by US/Eastern date (fixed UTC-4 approximation -- good enough for
    # a once-a-day reminder; the Schedule panel already has this same
    # server-local-time limitation for date bucketing).
    tomorrow = (datetime.now(timezone.utc) - timedelta(hours=4) + timedelta(days=1)).strftime("%Y-%m-%d")
    events = [e for e in result.get("events", []) if e.get("date") == tomorrow]
    if not events:
        return JSONResponse({"sent": 0, "detail": "no events tomorrow", "date": tomorrow})

    title = f"{len(events)} event{'s' if len(events) != 1 else ''} tomorrow"
    body = "; ".join(f"{e['time']} — {e['title']}" for e in events)
    sent = push.send_to_owner(title, body)
    return JSONResponse({"sent": sent, "date": tomorrow, "events": events})


@app.get("/api/brand")
def get_brand_api() -> JSONResponse:
    """Public. Returns current brand theme — accent color, derived light/dark, logo URL."""
    return JSONResponse(brand.get_brand())


@app.post("/api/brand")
async def set_brand_api(request: Request) -> JSONResponse:
    """Owner-only (behind access gate). Saves brand accent color and/or logo URL."""
    body = await request.json()
    return JSONResponse(brand.set_brand(
        accent=body.get("accent"),
        logo_url=body.get("logo_url"),
    ))


@app.get("/api/results")
def get_results(month: str = "") -> JSONResponse:
    """Owner-only. Proof-of-value summary: leads, bookings, conversations for one month."""
    return JSONResponse(results.get_monthly_summary(month))


@app.get("/api/export")
def export_data() -> JSONResponse:
    """Owner-only. Export all conversations + CRM data as JSON for backup/analysis."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    export = {
        "exported_at": now.isoformat(),
        "conversations": crm.get_all_history(limit=9999),
        "users": users.list_users() if crm.is_configured() else [],
    }
    return JSONResponse({
        "data": export,
        "filename": f"stinger-export-{now.strftime('%Y%m%d-%H%M%S')}.json"
    })


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
    # generic zapier_webhook_url above stays the fallback). Blank = untouched.
    zapier_webhook_url_facebook: str = ""
    zapier_webhook_url_instagram: str = ""
    zapier_webhook_url_youtube: str = ""
    zapier_webhook_url_tiktok: str = ""
    zapier_webhook_url_x: str = ""
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""
    openai_api_key: str = ""
    xai_api_key: str = ""
    media_image_provider: str = ""  # "chatgpt" or "grok" (for images)
    media_video_provider: str = ""  # "grok" (only option currently)
    automation_level: str = ""
    chat_model_default: str = ""  # MODEL_CHOICES slug
    # Per-business public website URLs. Stored as website__<brand>, surfaced to
    # Annabelle so she shares the right site with customers and uses it in
    # content. Blank = untouched (same convention as every other field here).
    website_dreamerie: str = ""
    website_suzy_d: str = ""
    website_bear_arms: str = ""
    website_peptides: str = ""


@app.get("/api/settings")
def get_settings(request: Request) -> JSONResponse:
    """Behind the access gate. Reports connection status and current config.
    Voice, model, and automation level are returned per-user when overrides exist;
    all other fields are global (owner-only to change). Secrets (app password,
    access code) are never echoed back -- only whether they're set/custom."""
    username = _get_session_username(request)
    password_is_default = False
    if crm.is_configured():
        owner = users.get_user("owner")
        if owner and not crm.get_setting("owner_password_changed", ""):
            password_is_default = True
    return JSONResponse({
        "gmail_address": emailer.get_gmail_address(),
        "gmail_connected": emailer.is_configured(),
        "search_cap": get_search_cap(),
        "search_count": crm.get_search_count(),
        "public_chat_cap": get_public_chat_cap(),
        "public_chat_count": crm.get_chat_count("public"),
        "owner_chat_cap": get_owner_chat_cap(),
        "owner_chat_count": crm.get_chat_count("owner"),
        "tts_voice": get_user_tts_voice(username),
        "tts_voice_options": TTS_VOICE_OPTIONS,
        "automation_level": get_user_automation_level(username),
        "chat_model_default": get_user_model_key(username),
        "zapier_webhook_url": crm.get_setting(social.WEBHOOK_KEY, "") if crm.is_configured() else "",
        "zapier_webhook_url_instagram": crm.get_setting(f"{social.WEBHOOK_KEY}_instagram", "") if crm.is_configured() else "",
        "zapier_webhook_url_youtube": crm.get_setting(f"{social.WEBHOOK_KEY}_youtube", "") if crm.is_configured() else "",
        "zapier_webhook_url_tiktok": crm.get_setting(f"{social.WEBHOOK_KEY}_tiktok", "") if crm.is_configured() else "",
        "model_options": [{"key": k, **v} for k, v in MODEL_CHOICES.items()],
        "access_code_is_custom": bool(crm.get_setting("access_code_override", "")),
        **{f"website_{b}": (crm.get_setting(f"website__{b}", "") if crm.is_configured() else "")
           for b in brand_identity.BRANDS},
        "calendar_connected": gcal.is_configured(),
        "calendar_configurable": bool(gcal.CLIENT_ID and gcal.CLIENT_SECRET),
        "social_connected": social.is_configured(),
        "social_platforms": social.connected_platforms(),
        "elevenlabs_connected": voice_eleven.is_configured(),
        "elevenlabs_voice_options": list(voice_eleven.DEFAULT_VOICES.keys()),
        "openai_connected": is_openai_configured(),
        "xai_connected": media_gen.is_grok_configured(),
        "media_image_provider": crm.get_setting("media_image_provider", "chatgpt") if crm.is_configured() else "chatgpt",
        "media_video_provider": crm.get_setting("media_video_provider", "grok") if crm.is_configured() else "grok",
        "password_is_default": password_is_default,
    })


@app.post("/api/settings")
def save_settings(req: SettingsUpdate, request: Request) -> JSONResponse:
    """Saves config to Airtable so changes take effect immediately -- no Render
    redeploy needed.  Blank fields are left untouched.

    Owner users: voice, model, and automation changes become the global default
    that new users inherit.  Staff users: those same changes are scoped to their
    own account only (per-user override), leaving the global defaults intact."""
    username = _get_session_username(request)
    user = users.get_user(username) if username else None
    # An access-code deployment (Susan's) has no per-user login: whoever holds
    # the code IS the owner. Without this, is_owner was always False for her and
    # EVERY admin setting silently no-op'd -- she could type, click Save, see
    # "Saved", and nothing persisted. Multi-user deployments set no cc_access,
    # so this grants nothing there.
    is_owner = ((user or {}).get("role") == "owner") or _access_authenticated(request)

    # Admin-only settings — only owners may change these.
    # Saved SYNCHRONOUSLY (sync=True) and failures collected: these are the
    # "click Save, walk away" settings, where the background writer's cheerful
    # "Saved." over a write that never landed is how the Zapier webhooks kept
    # reverting. A Save button can afford one Airtable round-trip; a false
    # success costs an afternoon.
    failed: List[str] = []

    def _save(key: str, value: str, label: str) -> None:
        if not crm.set_setting(key, value, sync=True):
            failed.append(label)

    if is_owner:
        if req.gmail_address.strip():
            _save(emailer.GMAIL_ADDRESS_KEY, req.gmail_address.strip(), "Gmail address")
        if req.gmail_app_password.strip():
            _save(emailer.GMAIL_APP_PASSWORD_KEY, req.gmail_app_password.strip(), "Gmail app password")
        for key, field, label in (
            ("cap_search_monthly", req.search_cap, "search cap"),
            ("cap_chat_public", req.public_chat_cap, "public chat cap"),
            ("cap_chat_owner", req.owner_chat_cap, "owner chat cap"),
        ):
            if field.strip():
                try:
                    _save(key, str(int(field.strip())), label)
                except ValueError:
                    pass
        if req.access_code.strip():
            _save("access_code_override", req.access_code.strip(), "access code")
        if req.zapier_webhook_url.strip():
            _save(social.WEBHOOK_KEY, req.zapier_webhook_url.strip(), "default webhook")
        for plat in ("facebook", "instagram", "youtube", "tiktok", "x"):
            val = getattr(req, f"zapier_webhook_url_{plat}", "").strip()
            if val:
                _save(f"{social.WEBHOOK_KEY}_{plat}", val, f"{plat} webhook")
        if req.elevenlabs_api_key.strip():
            _save(voice_eleven.API_KEY_SETTING, req.elevenlabs_api_key.strip(), "ElevenLabs key")
        if req.elevenlabs_voice_id.strip():
            vid = voice_eleven.DEFAULT_VOICES.get(req.elevenlabs_voice_id.strip(), req.elevenlabs_voice_id.strip())
            _save(voice_eleven.VOICE_ID_SETTING, vid, "ElevenLabs voice")
        if req.openai_api_key.strip():
            _save(OPENAI_API_KEY_SETTING, req.openai_api_key.strip(), "OpenAI key")
        if req.xai_api_key.strip():
            _save("xai_api_key", req.xai_api_key.strip(), "xAI key")
        if req.media_image_provider.strip() in ("chatgpt", "grok"):
            _save("media_image_provider", req.media_image_provider.strip(), "image provider")
        if req.media_video_provider.strip() in ("grok",):
            _save("media_video_provider", req.media_video_provider.strip(), "video provider")
        for b in brand_identity.BRANDS:
            raw = getattr(req, f"website_{b}", "")
            if raw.strip():
                _save(f"website__{b}", _normalize_url(raw), f"{b} website")

    # Per-user settings (voice, model, automation) -- any logged-in user saves
    # these to their own account; owners save globally so new users inherit them.
    if req.tts_voice.strip():
        if is_owner:
            crm.set_setting("tts_voice_override", req.tts_voice.strip())
        elif username:
            crm.set_user_setting(username, "tts_voice_override", req.tts_voice.strip())
    if req.automation_level.strip() in AUTOMATION_LEVELS:
        if is_owner:
            crm.set_setting("automation_level", req.automation_level.strip())
        elif username:
            crm.set_user_setting(username, "automation_level", req.automation_level.strip())
    if req.chat_model_default.strip() in MODEL_CHOICES:
        if is_owner:
            crm.set_setting("chat_model_default", req.chat_model_default.strip())
        elif username:
            crm.set_user_setting(username, "chat_model_default", req.chat_model_default.strip())
    if failed:
        # 502: the request was fine, the datastore write was not. Naming the
        # fields that failed is the difference between "not saving to Zap,
        # no idea why" and a fixable report.
        return JSONResponse({
            "ok": False,
            "detail": ("These didn't save (Airtable rejected the write): "
                       + ", ".join(failed)
                       + ". Check the /support page for the exact error."),
            "failed": failed,
        }, status_code=502)
    return JSONResponse({
        "ok": True,
        "gmail_connected": emailer.is_configured(),
        "social_connected": social.is_configured(),
        "social_platforms": social.connected_platforms(),
        "elevenlabs_connected": voice_eleven.is_configured(),
        "openai_connected": is_openai_configured(),
        "xai_connected": media_gen.is_configured(),
    })


# User management endpoints (owner only, behind access gate)


class AddUserRequest(BaseModel):
    username: str
    email: str
    password: str
    role: str = "owner"  # "owner" or "staff"


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


def _get_session_username(request: Request) -> Optional[str]:
    """Return the username for the current session, or None if unauthenticated."""
    token = request.cookies.get("cc_session")
    if not token:
        return None
    return users.verify_session_token(token)


def _get_session_role(request: Request) -> Optional[str]:
    """Return the role of the current session user, or None if unauthenticated."""
    username = _get_session_username(request)
    if not username:
        return None
    user = users.get_user(username)
    return user.get("role") if user else None


def _is_superadmin(request: Request) -> bool:
    """True only for the single operator account that may reset the deployment.
    Identified by SUPERADMIN_USERNAME (defaults to the bootstrap 'owner'). An
    access-code session with no username is never super-admin."""
    username = _authed_username(request)
    if not username:
        return False
    superadmin = os.environ.get("SUPERADMIN_USERNAME", "owner").strip()
    return username == superadmin


def _is_owner_request(request: Request) -> bool:
    """Owner check that works on BOTH auth systems.

    Same principle as the /api/settings fix (28 Jul): on an access-code
    deployment whoever holds the code IS the owner. Every user-management
    endpoint gated on the session role alone silently 403'd for Susan, which is
    why her Settings showed an "Invite a team member" form that could never
    add anyone -- she needs it to create Nick's login. Multi-user deployments
    never set cc_access, so this grants nothing there.
    """
    return _get_session_role(request) == "owner" or _access_authenticated(request)


@app.get("/api/users")
def get_users(request: Request) -> JSONResponse:
    """Owner-only. List all users."""
    if not _is_owner_request(request):
        return JSONResponse({"ok": False, "detail": "Owner access required"}, status_code=403)
    user_list = users.list_users()
    return JSONResponse({"ok": True, "users": user_list})


@app.post("/api/users")
def add_user(req: AddUserRequest, request: Request) -> JSONResponse:
    """Owner-only. Create a new user account."""
    if not _is_owner_request(request):
        return JSONResponse({"ok": False, "detail": "Owner access required"}, status_code=403)
    if not req.username or not req.email or not req.password:
        return JSONResponse({"ok": False, "detail": "Missing required fields"}, status_code=400)
    ok, reason = users.validate_username(req.username)
    if not ok:
        return JSONResponse({"ok": False, "detail": reason}, status_code=400)
    ok, reason = users.validate_password(req.password)
    if not ok:
        return JSONResponse({"ok": False, "detail": reason}, status_code=400)
    created, why = users.add_user(req.username, req.email, req.password, req.role)
    if created:
        # Set default user preferences
        if crm.is_configured():
            try:
                crm.set_user_setting(req.username, "chat_model_default", get_default_model_key())
                crm.set_user_setting(req.username, "automation_level", get_automation_level())
                crm.set_user_setting(req.username, "tts_voice_override", get_tts_voice())
            except Exception as e:
                log.warning(f"Could not set defaults for new user {req.username}: {e}")
        support.record_note("user_added", f"{req.username} ({req.role})", path="/api/users")
        return JSONResponse({"ok": True, "detail": "User created"})
    # Put the REAL reason on the support page too. The middleware only records
    # "POST /api/users -> 400"; without this, Vinny sees that Susan's invite
    # failed and still has to ask her what it said, which is the exact relay
    # the remote-support view exists to remove.
    support.record_note("user_add_failed", why or "no reason given", path="/api/users")
    return JSONResponse({"ok": False, "detail": why or "Could not create the user."}, status_code=400)


@app.delete("/api/users/{username}")
def delete_user(username: str, request: Request) -> JSONResponse:
    """Owner-only. Delete a user account."""
    if not _is_owner_request(request):
        return JSONResponse({"ok": False, "detail": "Owner access required"}, status_code=403)
    if users.delete_user(username):
        return JSONResponse({"ok": True, "detail": "User deleted"})
    return JSONResponse({"ok": False, "detail": "User not found"}, status_code=400)


@app.post("/api/users/{username}/reset-password")
def admin_reset_password(username: str, request: Request) -> JSONResponse:
    """Owner-only. Issue a new temporary password for a locked-out user.

    This existed nowhere before: /api/change-password needs the CURRENT password,
    and the only owner controls were create and delete. Deleting and recreating
    was the sole workaround, and it silently detaches the user's history --
    chats and per-user settings are keyed by username, so the recreated account
    only looks like the same one.

    The password is generated server-side rather than taken from the request
    body, so the plaintext exists in exactly one response and is never stored or
    logged. Hand it to the user out-of-band; they change it from Settings.
    """
    if not _is_owner_request(request):
        return JSONResponse({"ok": False, "detail": "Owner access required"}, status_code=403)

    user = users.get_user(username)
    if not user:
        return JSONResponse({"ok": False, "detail": "User not found"}, status_code=404)

    temp = password_reset.generate_temporary_password()
    ok, reason = users.validate_password(temp)
    if not ok:  # generator guarantees this passes; refuse rather than half-apply
        log.error(f"Generated temp password failed validation: {reason}")
        return JSONResponse({"ok": False, "detail": "Could not generate a password"}, status_code=500)
    if not users.update_user_password(username, temp):
        return JSONResponse({"ok": False, "detail": "Failed to update password"}, status_code=500)

    # The login lockout is per-IP, not per-user, so a reset does NOT clear it.
    # If they were locked out, the new password still fails until the window
    # expires -- say so, or the reset looks broken.
    now = time.time()
    locked_ips = sum(
        1 for att in _unlock_attempts.values()
        if len([t for t in att if now - t < UNLOCK_WINDOW_SECONDS]) >= UNLOCK_MAX_ATTEMPTS
    )
    note = ""
    if locked_ips:
        note = (f" Note: {locked_ips} address(es) are currently rate-limited from failed "
                f"logins. If that's them, the new password won't work until the "
                f"{UNLOCK_WINDOW_SECONDS // 60}-minute window expires.")

    log.info(f"Owner reset the password for user {username}")  # never log the value
    return JSONResponse({
        "ok": True,
        "username": username,
        "temporary_password": temp,
        "detail": ("Temporary password issued. Give it to them directly -- it is shown once "
                   "and is not stored anywhere in readable form. Ask them to change it from "
                   "Settings once they're back in." + note),
    })


@app.post("/api/change-password")
def change_password(req: ChangePasswordRequest, request: Request) -> JSONResponse:
    """Any logged-in user. Change their own password."""
    # Extract username from session token
    session_token = request.cookies.get("cc_session")
    if not session_token:
        return JSONResponse({"ok": False, "detail": "Not logged in"}, status_code=401)
    username = users.verify_session_token(session_token)
    if not username:
        return JSONResponse({"ok": False, "detail": "Invalid session"}, status_code=401)

    # Verify current password
    user = users.get_user(username)
    if not user or not users.verify_password(req.current_password, user["password_hash"]):
        return JSONResponse({"ok": False, "detail": "Current password is incorrect"}, status_code=401)

    # Validate new password complexity
    ok, reason = users.validate_password(req.new_password)
    if not ok:
        return JSONResponse({"ok": False, "detail": reason}, status_code=400)

    # Update password
    if not users.update_user_password(username, req.new_password):
        return JSONResponse({"ok": False, "detail": "Failed to update password"}, status_code=500)

    # Mark that password has been changed from default
    crm.set_setting("owner_password_changed", "true")

    return JSONResponse({"ok": True, "detail": "Password changed"})


class ResetMemoryRequest(BaseModel):
    confirm: str = ""  # must equal "RESET" -- guards against a stray click


@app.post("/api/reset-memory")
def reset_memory(req: ResetMemoryRequest, request: Request) -> JSONResponse:
    """Super-admin only. Wipe all customer data + client identity for a fresh
    handoff (run after Q.C.). Secrets and user accounts are preserved. Requires
    an exact "RESET" confirmation in the body."""
    if not _is_superadmin(request):
        return JSONResponse({"ok": False, "detail": "Super-admin access required"}, status_code=403)
    if (req.confirm or "").strip() != "RESET":
        return JSONResponse(
            {"ok": False, "detail": 'Confirmation failed -- type RESET to proceed.'},
            status_code=400)
    summary = crm.reset_customer_data()
    who = _authed_username(request) or "?"
    print(f"[RESET] Customer data reset by super-admin '{who}': {summary}")
    status = 200 if summary.get("ok") else 500
    return JSONResponse(summary, status_code=status)


# Louden Bonded Pools KPI dashboard (owner only, behind access gate)


@app.get("/api/calendar/upcoming")
def calendar_upcoming(days: int = 30) -> JSONResponse:
    """Owner-only. Upcoming events on the connected calendar for the Schedule panel."""
    return JSONResponse(gcal.list_upcoming_events(days=days))


@app.get("/api/calendar/connect")
def calendar_connect() -> JSONResponse:
    """Owner-only. Returns the Google authorization URL to start the OAuth flow.
    The owner opens this URL, grants access, and Google redirects back to
    /auth/google-callback with a code we exchange for a token."""
    flow = gcal.get_oauth_flow()
    if not flow:
        return JSONResponse(
            {"error": "Google Calendar isn't configured on the server (missing client ID/secret)."},
            status_code=400,
        )
    flow.redirect_uri = gcal.REDIRECT_URI
    state = _make_oauth_state()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state,
    )
    return JSONResponse({"auth_url": auth_url})


@app.get("/auth/google-callback")
def google_callback(request: Request) -> HTMLResponse:
    """Public (gate-exempt): Google redirects here after the owner authorizes.
    We verify the CSRF state token, exchange the code, and store the credential."""
    state = request.query_params.get("state", "")
    if not _verify_oauth_state(state):
        return HTMLResponse("<h2>Invalid state token — possible CSRF. Please try connecting again.</h2>", status_code=400)
    code = request.query_params.get("code")
    if not code:
        return HTMLResponse("<h2>No authorization code received. Try again.</h2>", status_code=400)
    flow = gcal.get_oauth_flow()
    if not flow:
        return HTMLResponse("<h2>Calendar not configured on the server.</h2>", status_code=400)
    flow.redirect_uri = gcal.REDIRECT_URI
    try:
        flow.fetch_token(code=code)
        gcal.store_token(flow.credentials.to_json())
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;text-align:center;padding:60px'>"
            "<h2>Google Calendar connected!</h2>"
            "<p>You can close this tab and return to the Command Center.</p>"
            "</body></html>"
        )
    except Exception as e:
        return HTMLResponse(f"<h2>Couldn't connect: {e}</h2>", status_code=400)


@app.get("/api/drive/connect")
def drive_connect() -> JSONResponse:
    """Owner-only. Returns the Google OAuth URL for Drive.readonly scope."""
    flow = files_gdrive.get_oauth_flow()
    if not flow:
        return JSONResponse(
            {"error": "Google Drive isn't configured (missing GOOGLE_CLIENT_ID/SECRET)."},
            status_code=400,
        )
    flow.redirect_uri = files_gdrive.REDIRECT_URI
    state = _make_oauth_state()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state,
    )
    return JSONResponse({"auth_url": auth_url})


@app.get("/auth/drive-callback")
def drive_callback(request: Request) -> HTMLResponse:
    """Public (gate-exempt). Google redirects here after the owner authorizes Drive."""
    state = request.query_params.get("state", "")
    if not _verify_oauth_state(state):
        return HTMLResponse("<h2>Invalid state token — possible CSRF. Try again.</h2>", status_code=400)
    code = request.query_params.get("code")
    if not code:
        return HTMLResponse("<h2>No authorization code received. Try again.</h2>", status_code=400)
    flow = files_gdrive.get_oauth_flow()
    if not flow:
        return HTMLResponse("<h2>Drive not configured on the server.</h2>", status_code=400)
    flow.redirect_uri = files_gdrive.REDIRECT_URI
    try:
        flow.fetch_token(code=code)
        files_gdrive.store_token(flow.credentials.to_json())
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;text-align:center;padding:60px'>"
            "<h2>Google Drive connected!</h2>"
            "<p>You can close this tab and return to the Command Center.</p>"
            "</body></html>"
        )
    except Exception as e:
        return HTMLResponse(f"<h2>Couldn't connect Drive: {e}</h2>", status_code=400)


@app.get("/api/dropbox/connect")
def dropbox_connect() -> JSONResponse:
    """Owner-only. Returns the Dropbox OAuth authorize URL."""
    url = files_dropbox.authorize_url()
    if not url:
        return JSONResponse(
            {"error": "Dropbox isn't configured (missing DROPBOX_APP_KEY/DROPBOX_APP_SECRET). "
                      "You can also skip OAuth and set DROPBOX_ACCESS_TOKEN directly for testing."},
            status_code=400,
        )
    return JSONResponse({"auth_url": url})


@app.get("/dropbox/callback")
def dropbox_callback(request: Request) -> HTMLResponse:
    """Public (gate-exempt). Dropbox redirects here after authorization."""
    code = request.query_params.get("code", "")
    if not code:
        return HTMLResponse("<h2>No authorization code received. Try again.</h2>", status_code=400)
    try:
        payload = files_dropbox.exchange_code(code)
        files_dropbox.store_oauth_result(payload)
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;text-align:center;padding:60px'>"
            "<h2>Dropbox connected!</h2>"
            "<p>You can close this tab and return to the Command Center.</p>"
            "</body></html>"
        )
    except Exception as e:
        return HTMLResponse(f"<h2>Couldn't connect Dropbox: {e}</h2>", status_code=400)


@app.get("/lightspeed/connect")
def lightspeed_connect() -> Response:
    """Owner-only (behind the login gate). Redirects to Lightspeed's authorize page."""
    client_id = os.environ.get("LIGHTSPEED_CLIENT_ID", "")
    if not client_id:
        return HTMLResponse("<h2>LIGHTSPEED_CLIENT_ID isn't set in Render yet.</h2>", status_code=400)
    state = _make_oauth_state()
    url = (
        "https://cloud.lightspeedapp.com/auth/oauth/authorize"
        f"?response_type=code&client_id={client_id}&scope=employee:all&state={state}"
    )
    return RedirectResponse(url)


@app.get("/lightspeed/callback")
def lightspeed_callback(request: Request) -> HTMLResponse:
    """Public (gate-exempt): Lightspeed redirects here after the owner authorizes.
    Exchanges the code for tokens and displays them for pasting into Render env vars."""
    import html as _html
    if not _verify_oauth_state(request.query_params.get("state", "")):
        return HTMLResponse("<h2>Invalid state token. Start again from /lightspeed/connect.</h2>", status_code=400)
    code = request.query_params.get("code", "")
    if not code:
        return HTMLResponse("<h2>No authorization code received. Try again from /lightspeed/connect.</h2>", status_code=400)
    client_id = os.environ.get("LIGHTSPEED_CLIENT_ID", "")
    client_secret = os.environ.get("LIGHTSPEED_CLIENT_SECRET", "")
    if not (client_id and client_secret):
        return HTMLResponse("<h2>LIGHTSPEED_CLIENT_ID / LIGHTSPEED_CLIENT_SECRET aren't set in Render.</h2>", status_code=400)
    resp = httpx.post(
        "https://cloud.lightspeedapp.com/oauth/access_token.php",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
        },
        timeout=20,
    )
    if resp.status_code != 200:
        return HTMLResponse(
            f"<h2>Token exchange failed ({resp.status_code})</h2><pre>{_html.escape(resp.text)}</pre>",
            status_code=400,
        )
    tokens = resp.json()
    access_token = tokens.get("access_token", "")
    refresh_token = tokens.get("refresh_token", "")
    business_id = ""
    try:
        acct = httpx.get(
            "https://api.lightspeedapp.com/API/V3/Account.json",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=20,
        )
        if acct.status_code != 200:
            acct = httpx.get(
                "https://api.lightspeedapp.com/API/Account.json",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=20,
            )
        if acct.status_code == 200:
            account = acct.json().get("Account", {})
            if isinstance(account, list):
                account = account[0] if account else {}
            business_id = str(account.get("accountID", ""))
    except Exception:
        pass
    rows = "".join(
        f"<p style='margin:18px 0'><b>{name}</b><br>"
        f"<code style='display:block;background:#f4f4f4;padding:10px;border-radius:6px;"
        f"word-break:break-all;user-select:all'>{_html.escape(value) or '(not found — see note below)'}</code></p>"
        for name, value in [
            ("LIGHTSPEED_ACCESS_TOKEN", access_token),
            ("LIGHTSPEED_REFRESH_TOKEN", refresh_token),
            ("LIGHTSPEED_BUSINESS_ID", business_id),
        ]
    )
    return HTMLResponse(
        "<html><body style='font-family:sans-serif;max-width:720px;margin:40px auto;padding:0 20px'>"
        "<h2>Lightspeed connected!</h2>"
        "<p>Copy each value below into the matching Render environment variable, "
        "then click <b>Save, rebuild, and deploy</b>. Close this tab when done — "
        "these values are secrets.</p>"
        f"{rows}"
        "<p style='color:#666;font-size:14px'>If Business ID shows as not found, it's visible in "
        "Lightspeed Back Office under Settings once logged in.</p>"
        "</body></html>"
    )


# Per-IP sliding-window limiter for the public widget — stops a bot from
# draining the monthly cap in seconds. Resets on restart (acceptable for
# single-instance; the monthly cap is the real backstop).
_PUBLIC_CHAT_WINDOW = 60   # seconds
_PUBLIC_CHAT_LIMIT = 10    # requests per IP per window
_public_chat_times: Dict[str, list] = {}
_public_chat_last_gc = 0.0


def _gc_rate_limiter() -> None:
    """Evict IPs whose last request was over 2× the window ago to prevent unbounded growth."""
    global _public_chat_last_gc
    now = time.time()
    if now - _public_chat_last_gc < 300:  # GC at most every 5 minutes
        return
    _public_chat_last_gc = now
    stale = [ip for ip, ts in _public_chat_times.items() if not ts or now - ts[-1] > _PUBLIC_CHAT_WINDOW * 2]
    for ip in stale:
        del _public_chat_times[ip]


@app.get("/api/agent-name")
def agent_name() -> JSONResponse:
    """Return the name Susan has chosen for the assistant, if any."""
    return JSONResponse({"name": crm.get_setting(AGENT_NAME_KEY) or None})


@app.get("/api/events")
def get_events(business: str = "") -> JSONResponse:
    """Owner-only. List events, optionally filtered to one business."""
    return JSONResponse({"events": events.list_events_raw(business)})


@app.post("/api/setup")
def setup_first_user(req: LoginRequest) -> JSONResponse:
    """One-time bootstrap: create the first admin user if no users exist yet."""
    existing = users.list_users()
    if existing:
        return JSONResponse({"ok": False, "detail": "Setup already complete. Use /api/login."}, status_code=403)
    if not req.username or not req.password:
        return JSONResponse({"ok": False, "detail": "Username and password required"}, status_code=400)
    ok, why = users.add_user(req.username, "admin@dreamerie.com", req.password, role="owner")
    if not ok:
        return JSONResponse({"ok": False, "detail": why or "Failed to create user"}, status_code=500)
    token = users.create_session_token(req.username)
    resp = JSONResponse({"ok": True, "detail": f"Account created for {req.username}. You are now logged in."})
    resp.set_cookie("cc_session", token, max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax", secure=True)
    return resp


@app.post("/api/public-chat", response_model=ChatResponse)
def public_chat(req: ChatRequest, request: Request) -> ChatResponse:
    """Customer-facing chat for the embeddable website widget. Not gated.
    Uses the public persona + limited tools (answer + capture leads)."""
    _gc_rate_limiter()
    ip = _client_ip(request)
    now = time.time()
    times = [t for t in _public_chat_times.get(ip, []) if now - t < _PUBLIC_CHAT_WINDOW]
    if len(times) >= _PUBLIC_CHAT_LIMIT:
        return ChatResponse(
            reply="You're sending messages a bit fast — give me a moment and try again.",
            delegated_to=[],
        )
    times.append(now)
    _public_chat_times[ip] = times
    req.clean()
    try:
        return run_main_brain(req.message, req.history,
                              build_public_prompt(_current_agent_name()) + _now_context(),
                              PUBLIC_TOOLS,
                               persona="public", request_id=req.request_id)
    finally:
        _clear_cancelled(req.request_id)


@app.get("/widget")
def widget() -> FileResponse:
    """Serve the public, embeddable chat widget (for the Wix site)."""
    return FileResponse("static/widget.html")


@app.get("/privacy")
def privacy() -> FileResponse:
    """Public privacy notice, linked from the widget footer."""
    return FileResponse("static/privacy.html")


# /terms is gone: the route served a file that was never ported, so it 500'd
# on every hit. The flagship's terms.html is a legal document naming Ohh
# BeeHave LLC / Port St. Lucie -- the WRONG entity for Susan's businesses, so
# it must not be copied here. If Twilio A2P is ever registered for this
# deployment, write a real ToS for HER entity first, restore the route, and
# re-add "/terms" to the access_gate exempt list.


# /capabilities (the Stinger marketing deck) is gone: the file was never
# ported, nothing here links to it, and the route 500'd on every hit.


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots_txt() -> str:
    """No public marketing site on this deployment -- keep crawlers out."""
    return "User-agent: *\nDisallow: /\n"


# ── TEXT-TO-SPEECH ──────────────────────────────────────────────────────────
# Restored after being dropped in the multi-company port while the UI that
# calls it stayed (the /api/unlock pattern again): index.html POSTs every
# spoken chunk here, so without this route Annabelle's voice was silently
# dead -- every clip fetch 404'd, and the Settings voice picker saved a
# preference nothing read.

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


async def _edge_tts(text: str, voice: str = "") -> bytes:
    """Free Microsoft Edge neural TTS -> MP3 bytes."""
    communicate = edge_tts.Communicate(text, voice or get_tts_voice(), rate=TTS_RATE)
    audio = bytearray()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio.extend(chunk["data"])
    return bytes(audio)


@app.post("/api/tts")
async def tts(req: TTSRequest, request: Request) -> Response:
    """Return spoken MP3 for the text. Quality-ordered fallback chain:
    ElevenLabs (paid, best quality, self-serve connect) -> Grok (XAI_API_KEY
    env var) -> free Edge TTS. Each stage falls through to the next on any
    failure rather than going silent. Voice is resolved per-user so a second
    account can have its own preferred voice."""
    username = _get_session_username(request)
    text = req.text.strip()[:3000]
    if not text:
        return Response(status_code=400)
    if voice_eleven.is_configured():
        try:
            data = await voice_eleven.synthesize(text)
            return Response(data, media_type="audio/mpeg", headers={"X-TTS-Engine": "elevenlabs"})
        except Exception as e:
            print(f"[tts] ElevenLabs failed, trying next engine: {type(e).__name__}: {e}", flush=True)
    if XAI_API_KEY:
        try:
            data = await _grok_tts(text)
            return Response(data, media_type="audio/mpeg", headers={"X-TTS-Engine": "grok"})
        except Exception as e:
            # Fall back to the free voice rather than going silent, but log why.
            print(f"[tts] Grok TTS failed, using Edge fallback: {type(e).__name__}: {e}", flush=True)
            voice = get_user_tts_voice(username)
            data = await _edge_tts(text, voice)
            return Response(data, media_type="audio/mpeg", headers={"X-TTS-Engine": "edge-fallback"})
    voice = get_user_tts_voice(username)
    return Response(await _edge_tts(text, voice), media_type="audio/mpeg", headers={"X-TTS-Engine": "edge"})


@app.get("/api/leads")
def get_leads(request: Request, limit: int = 20) -> JSONResponse:
    """Return recent CRM leads. Auth enforced by access_gate middleware."""
    limit = min(limit, 100)  # cap to prevent hammering Airtable
    if not crm.is_configured():
        return JSONResponse({"leads": [], "error": "Airtable not configured"})
    try:
        records = crm.get_leads_raw(limit=limit)
        return JSONResponse({"leads": records})
    except Exception as e:
        return JSONResponse({"leads": [], "error": str(e)}, status_code=500)


@app.get("/api/assets")
def get_assets_list(limit: int = 30, media_type: str = "") -> JSONResponse:
    """Return recent media assets for the media dock. Auth enforced by access_gate."""
    limit = min(limit, 50)
    return JSONResponse({"assets": assets.list_assets_raw(limit=limit, media_type=media_type)})


class AssetIn(BaseModel):
    name: str = ""
    url: str = ""
    media_type: str = "Photo"
    tags: str = ""
    notes: str = ""


class AssetsPushRequest(BaseModel):
    assets: List[AssetIn] = []


@app.post("/api/assets")
def push_assets(req: AssetsPushRequest, request: Request) -> JSONResponse:
    """Owner-only. Write assets straight into the library.

    Asking Annabelle in chat to "save these assets" is a REQUEST -- she may
    answer directly without ever calling save_asset, and the library stays
    empty while the transcript looks like it worked. This writes them, and
    reports per-asset what actually happened, so loading a client packet is a
    verifiable operation instead of a hopeful one.
    """
    if not _is_owner_request(request):
        return JSONResponse({"ok": False, "detail": "Owner access required"}, status_code=403)
    if not req.assets:
        return JSONResponse({"ok": False, "detail": "No assets given."}, status_code=400)
    results = []
    for a in req.assets:
        ok, msg = assets.add_asset_checked(a.name, a.url, a.media_type, a.tags, a.notes)
        results.append({"name": a.name, "ok": ok, "detail": msg})
        if not ok:
            support.record_note("asset_push_failed", f"{a.name}: {msg}", path="/api/assets")
    saved = sum(1 for r in results if r["ok"])
    # ok is True only when EVERY asset landed -- a partial write reported as
    # success is how a half-loaded packet looks finished.
    return JSONResponse(
        {"ok": saved == len(results), "saved": saved, "total": len(results), "results": results},
        status_code=200 if saved == len(results) else 207,
    )


@app.get("/api/diagnostic")
def api_diagnostic() -> JSONResponse:
    """Owner-only. Deep probe of every integration — actually pings Stripe,
    HubSpot, Twilio, Airtable, etc. and returns per-service ok/latency/error.
    Backs both the /diagnostic UI page and Annabelle's run_diagnostic tool."""
    return JSONResponse(diagnostic.run_all())


@app.get("/diagnostic", response_class=HTMLResponse)
def diagnostic_page() -> FileResponse:
    """Owner-only. Human-readable status board for every integration."""
    return FileResponse("static/diagnostic.html")


# ── Remote support ───────────────────────────────────────────────────────────
# /diagnostic answers "are the integrations wired up". These answer the
# different question that actually cost a day: "what has this app been doing,
# and did it just restart underneath her". Both are behind the same gate.

@app.get("/api/support/report")
def api_support_report(limit: int = 200) -> JSONResponse:
    """Owner-only. Recent faults, uptime, and the serving revision.

    No network calls -- it has to answer when the network is the problem.
    Contains no request bodies, no query strings and no chat content by
    construction; see app/support.py.
    """
    return JSONResponse(support.report(limit=max(1, min(limit, support.MAX_EVENTS))))


class ClientEvent(BaseModel):
    """What a browser is allowed to tell us. Everything else it sends is
    ignored -- the beacon is ungated, so this model is the whole trust
    boundary. Lengths are re-capped again in support._scrub()."""
    model_config = ConfigDict(extra="ignore")

    kind: str = "client_error"
    path: str = ""
    method: str = ""
    status: Optional[int] = None
    detail: str = ""
    ua: str = ""


@app.post("/api/support/client-event")
def api_support_client_event(ev: ClientEvent, request: Request) -> JSONResponse:
    """Ungated. The browser reporting something that broke on HER side.

    A request that times out or never resolves produces no server log line at
    all -- that is precisely what "trouble reaching server" is. Without this
    endpoint that whole class of failure is invisible to everyone except the
    person staring at the spinner.
    """
    accepted = support.record_client_event(
        ev.model_dump(), ip=_client_ip(request),
        authenticated=_identity_scope(request) is not None,
    )
    # 202 either way: the beacon must never make a broken page look more broken,
    # and telling an unauthenticated caller whether it was throttled tells them
    # nothing useful and gives a prober a signal.
    return JSONResponse({"ok": accepted}, status_code=202)


@app.get("/support", response_class=HTMLResponse)
def support_page() -> FileResponse:
    """Owner-only. The read-only remote-support view."""
    return FileResponse("static/support.html")


@app.get("/api/features")
def get_features_status() -> JSONResponse:
    """Owner-only. Returns which features are enabled/disabled and why.
    Used by the UI to show 'pending configuration' notices."""
    config_status = config_check.get_configured_integrations()
    disabled_report = config_check.get_disabled_tools_report()

    return JSONResponse({
        "integrations": {
            name: {
                "enabled": is_config,
                "status": reason,
            }
            for name, (is_config, reason) in config_status.items()
        },
        "disabled_tools_by_reason": disabled_report,
        "pending_configuration": [
            {
                "integration": name,
                "action": reason,
            }
            for name, (is_config, reason) in config_status.items()
            if not is_config
        ]
    })


@app.get("/api/health")
def health_check() -> JSONResponse:
    """Owner-only. Returns connection status for every integration, and a checklist
    of what still needs to be configured. Useful for monitoring and for Annabelle
    to answer 'what's connected?' questions."""
    from . import sms

    # Get integration status
    config_status = config_check.get_configured_integrations()
    integrations = {name: is_config for name, (is_config, _) in config_status.items()}

    # Get the reason message for each integration
    reasons = {name: reason for name, (_, reason) in config_status.items()}

    # Get disabled tools report
    disabled_report = config_check.get_disabled_tools_report()

    # Count stats
    total_tools = len(config_check.TOOL_DEPENDENCIES)
    enabled_tools = len([t for t in DELEGATION_TOOLS if t.get("name") in config_check.get_enabled_tool_names()])
    disabled_tools = total_tools - enabled_tools

    return JSONResponse({
        "status": "ok",
        "model": resolve_model(),
        "integrations": {
            "anthropic": True,
            "airtable": crm.is_configured(),
            "gmail": emailer.is_configured(),
            "calendar": integrations.get("calendar", False),
            "social_zapier": social.is_configured(),
            "elevenlabs": voice_eleven.is_configured(),
            "stripe": integrations.get("stripe", False),
            "hubspot": integrations.get("hubspot", False),
            "twilio": integrations.get("twilio", False),
            "xai_media_gen": media_gen.is_configured(),
            "push_notifications": push.is_configured(),
        },
        "configuration": {
            "integration_details": reasons,
            "tools_enabled": enabled_tools,
            "tools_disabled": disabled_tools,
            "disabled_tools_by_reason": disabled_report,
            "pending_actions": [
                {"integration": k, "action": v}
                for k, v in reasons.items()
                if not config_status[k][0]  # only unconfigured integrations
            ]
        }
    })


# ── STRIPE WEBHOOK ────────────────────────────────────────────────────────────

@app.post("/api/webhooks/stripe")
async def stripe_webhook(request: Request) -> JSONResponse:
    """Gate-exempt. Stripe sends signed events here (payment succeeded, failed,
    subscription updated). HMAC-verified before any action is taken."""
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    event = stripe_billing.verify_webhook(payload, sig)
    if event is None:
        return JSONResponse({"ok": False, "detail": "Invalid signature"}, status_code=400)

    event_type = event.get("type", "")
    data_obj = event.get("data", {}).get("object", {})

    if event_type == "invoice.paid":
        customer_email = data_obj.get("customer_email", "unknown")
        amount = data_obj.get("amount_paid", 0) / 100
        note = f"Stripe payment received: ${amount:.2f} from {customer_email}."
        if crm.is_configured():
            crm.save_turn("system", note)
        from . import sms
        if sms.is_configured() and os.environ.get("OWNER_PHONE"):
            try:
                sms.send(to=os.environ["OWNER_PHONE"], body=f"💰 Payment received: ${amount:.2f} from {customer_email}")
            except Exception as e:
                log.warning("Stripe webhook SMS notification failed: %s", e)

    elif event_type == "invoice.payment_failed":
        customer_email = data_obj.get("customer_email", "unknown")
        note = f"Stripe payment FAILED for {customer_email}."
        if crm.is_configured():
            crm.save_turn("system", note)

    elif event_type in ("customer.subscription.updated", "customer.subscription.deleted"):
        sub_id = data_obj.get("id", "?")
        status = data_obj.get("status", "?")
        note = f"Stripe subscription {sub_id} → {status}."
        if crm.is_configured():
            crm.save_turn("system", note)

    return JSONResponse({"ok": True, "type": event_type})


# ── PROPOSAL ENDPOINTS ───────────────────────────────────────────────────────

class ProposalSignRequest(BaseModel):
    proposal_id: str = ""
    signer_name: str
    signer_email: str
    signature_data_url: str = ""  # base64 PNG from canvas
    client_name: str = ""
    project_title: str = ""
    total: str = ""


_SIGN_WINDOW = 600   # 10 min sliding window
_SIGN_LIMIT = 5      # signatures per IP per window (real clients need one or two, not five)
_sign_times: Dict[str, list] = {}


@app.post("/api/proposals/sign")
def sign_proposal(req: ProposalSignRequest, request: Request) -> JSONResponse:
    """
    Record a signed proposal. Logs to CRM, optionally sends via DocuSign.
    Called by the proposal.html viewer when the client accepts.
    Gate-exempt: the client isn't logged in.
    """
    # Per-IP throttle -- open audit finding #6. Same sliding-window pattern
    # already used for /api/public-chat and /api/unlock.
    ip = _client_ip(request)
    now = time.time()
    times = [t for t in _sign_times.get(ip, []) if now - t < _SIGN_WINDOW]
    if len(times) >= _SIGN_LIMIT:
        return JSONResponse({"ok": False, "detail": "Too many signature attempts. Please wait a few minutes."}, status_code=429)
    times.append(now)
    _sign_times[ip] = times

    import datetime as _dt
    note = (
        f"Proposal SIGNED by {req.signer_name} <{req.signer_email}> "
        f"on {_dt.datetime.now(_dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}. "
        f"Project: {req.project_title}. Total: {req.total}."
    )
    if crm.is_configured():
        crm.save_turn("system", note)
    # Try HubSpot if configured
    hubspot_result = None
    if os.environ.get("HUBSPOT_ACCESS_TOKEN"):
        try:
            hubspot_result = hubspot.capture_lead(
                name=req.signer_name,
                email=req.signer_email,
                service=req.project_title,
                notes=note,
                deal_stage="contractsent",
            )
        except Exception as e:
            print(f"[proposals/sign] HubSpot error: {e}", flush=True)
    return JSONResponse({
        "ok": True,
        "detail": f"Proposal accepted. Signature recorded for {req.signer_name}.",
        "hubspot": bool(hubspot_result),
    })


@app.get("/proposal")
def proposal_page() -> FileResponse:
    """Serve the client-facing proposal viewer (gate-exempt)."""
    return FileResponse("static/proposal.html")


# ── BUILDERTREND WEBHOOK ──────────────────────────────────────────────────────



# The Buildertrend webhook route that used to live here is gone: no
# buildertrend module exists in this repo (never ported from the Louden
# build), so the moment its secret was configured, every delivered event
# 500'd on a NameError. None of Susan's four businesses use Buildertrend.


# ── HUBSPOT LEAD CAPTURE (direct API shortcut) ────────────────────────────────

class HubSpotLeadRequest(BaseModel):
    name: str
    email: str = ""
    phone: str = ""
    company: str = ""
    service: str = ""
    notes: str = ""


@app.post("/api/hubspot/lead")
def hubspot_lead(req: HubSpotLeadRequest) -> JSONResponse:
    """Owner-only. Manually push a lead into HubSpot CRM."""
    if not os.environ.get("HUBSPOT_ACCESS_TOKEN"):
        return JSONResponse({"ok": False, "detail": "HubSpot not configured"}, status_code=400)
    try:
        result = hubspot.capture_lead(
            name=req.name,
            email=req.email,
            phone=req.phone,
            company=req.company,
            service=req.service,
            notes=req.notes,
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "detail": f"HubSpot API error: {exc}"}, status_code=502)
    return JSONResponse({"ok": True, "contact_id": result["contact"]["id"],
                         "deal_id": result["deal"]["id"]})


class DevTicketRequest(BaseModel):
    request: str
    urgency: str = "normal"


@app.post("/api/dev/ticket")
def dev_ticket(req: DevTicketRequest) -> JSONResponse:
    """Autonomous build system: webhook endpoint for dev tickets.

    Annabelle or a scheduled job posts a ticket here. If dev_approval_level
    is "manual", just log and return. Otherwise, invoke Claude to handle it.
    """
    if not crm.is_configured():
        return JSONResponse({"ok": False, "detail": "CRM not configured"}, status_code=400)

    # Get the approval level from settings
    approval_level = crm.get_setting("dev_approval_level", "manual")

    # Always log the ticket to Airtable
    ticket_id = f"ticket_{int(time.time() * 1000)}"
    crm.save_dev_agent_log(
        ticket_id=ticket_id,
        action=req.request[:500],
        approval_level=approval_level,
        result="Logged"
    )

    # Every level now logs-and-notifies only. The old low_risk/full_auto
    # branch called the bare Anthropic API with NO tools, no filesystem, no
    # repo -- Claude cannot execute anything that way -- then wrote whatever
    # JSON the model invented ("success": true, "changed_files": [...]) into
    # the Dev Agent Log as if the work had happened. A log of changes that
    # never happened is the "green suite worse than red" failure class.
    # Real autonomous execution needs an agent harness, not messages.create.
    return JSONResponse({"ok": True, "ticket_id": ticket_id, "status": "logged",
                        "detail": "Ticket logged. Review and execute in Claude Code."})


# Restored after being dropped in the multi-company port: chat mints
# /artifact/{slug} links for every proposal/audit/content doc (and the
# Artifacts panel offers Open/Copy Link), but no route served them -- every
# link, including ones copied to clients, 404'd. crm.get_artifact existed
# with zero callers. Ported from the flagship, rebranded.
@app.get("/artifact/{slug}", response_class=HTMLResponse)
def artifact_page(slug: str) -> HTMLResponse:
    """Gate-exempt. Renders a document Annabelle generated (proposal, audit,
    long-form content) as a plain readable page -- the link the Artifacts
    panel gives you to copy/share."""
    art = crm.get_artifact(slug)
    if not art:
        return HTMLResponse("<h1>Not found</h1><p>This link is invalid or has expired.</p>", status_code=404)
    import html as _html
    title = _html.escape(art.get("title", "Document"))
    # Same title, safely embeddable in the read-aloud script below.
    title_js = json.dumps(art.get("title", "Document"))
    raw = art.get("content", "") or ""
    try:
        import markdown as _md
        import bleach as _bleach
        html_body = _md.markdown(raw, extensions=["extra", "sane_lists", "nl2br"])
        allowed_tags = ["p", "br", "hr", "strong", "em", "b", "i", "u", "s",
                        "h1", "h2", "h3", "h4", "h5", "h6",
                        "ul", "ol", "li", "blockquote", "pre", "code",
                        "a", "img", "table", "thead", "tbody", "tr", "th", "td",
                        "div", "span"]
        allowed_attrs = {"a": ["href", "title", "target", "rel"],
                         "img": ["src", "alt", "title", "width", "height"],
                         "*": ["class"]}
        body_html = _bleach.clean(html_body, tags=allowed_tags, attributes=allowed_attrs,
                                  protocols=["http", "https", "mailto", "data"], strip=True)
    except Exception:
        body_html = f"<pre>{_html.escape(raw)}</pre>"
    page = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — The Dreamerie</title>
<style>
body {{ margin:0; padding:40px 20px; background:#0d0d10; color:#e6e2d6; font-family:Inter,-apple-system,sans-serif; line-height:1.6; }}
.doc {{ max-width:760px; margin:0 auto; background:rgba(20,19,16,0.92); border:1px solid rgba(196,150,230,0.25); border-radius:14px; padding:36px 44px; }}
.doc h1.doc-title {{ font-size:22px; letter-spacing:0.04em; color:#e0b8f0; margin:0 0 24px; padding-bottom:14px; border-bottom:1px solid rgba(196,150,230,0.2); }}
.doc h1, .doc h2, .doc h3 {{ color:#e0b8f0; margin:22px 0 10px; line-height:1.3; }}
.doc h1 {{ font-size:20px; }} .doc h2 {{ font-size:17px; }} .doc h3 {{ font-size:15px; }}
.doc p, .doc li {{ font-size:15px; }}
.doc ul, .doc ol {{ padding-left:22px; margin:10px 0; }}
.doc a {{ color:#e0b8f0; }}
.doc img {{ max-width:100%; height:auto; border-radius:8px; margin:10px 0; }}
.doc pre, .doc code {{ background:rgba(0,0,0,0.35); border-radius:6px; padding:2px 6px; font-family:ui-monospace,Menlo,monospace; font-size:13px; }}
.doc pre {{ padding:14px; white-space:pre-wrap; word-break:break-word; }}
.doc table {{ border-collapse:collapse; width:100%; margin:12px 0; }}
.doc th, .doc td {{ border:1px solid rgba(196,150,230,0.2); padding:8px 10px; text-align:left; font-size:14px; }}
.doc th {{ background:rgba(196,150,230,0.08); color:#e0b8f0; }}
.doc blockquote {{ border-left:3px solid rgba(196,150,230,0.4); padding-left:14px; margin:12px 0; color:#c8c4b8; }}

/* Toolbar: read-aloud + save-as-PDF. Hidden when printing. */
.doc-tools {{ display:flex; flex-wrap:wrap; gap:10px; margin:0 0 22px; }}
.doc-tools button {{ font-family:inherit; font-size:13px; font-weight:600; padding:9px 16px; border-radius:9px;
  cursor:pointer; border:1px solid rgba(196,150,230,0.4); background:rgba(196,150,230,0.1); color:#e0b8f0; }}
.doc-tools button:hover {{ background:rgba(196,150,230,0.2); }}
.doc-tools button[disabled] {{ opacity:0.4; cursor:default; }}
.doc-tools .primary {{ background:linear-gradient(135deg,#8a4fb0,#b87ad9); border:none; color:#0a0a0c; }}
#readStatus {{ align-self:center; font-size:12px; color:#9a9486; }}

/* Print / Save-as-PDF: drop the dark theme for a clean white document. */
@media print {{
  @page {{ margin:18mm 16mm; }}
  body {{ background:#fff; color:#111; padding:0; }}
  .doc {{ background:#fff; border:none; border-radius:0; padding:0; max-width:none; }}
  .doc-tools {{ display:none !important; }}
  .doc h1.doc-title {{ color:#111; border-bottom:2px solid #b87ad9; font-size:24px; }}
  .doc h1, .doc h2, .doc h3 {{ color:#111; page-break-after:avoid; }}
  .doc a {{ color:#111; text-decoration:underline; }}
  .doc pre, .doc code {{ background:#f1eaf5; color:#111; }}
  .doc th {{ background:#f1eaf5; color:#111; }}
  .doc th, .doc td {{ border:1px solid #bbb; }}
  .doc blockquote {{ border-left:3px solid #b87ad9; color:#333; }}
  .doc table, .doc pre, .doc blockquote, .doc img {{ page-break-inside:avoid; }}
}}
</style></head>
<body><div class="doc">
<h1 class="doc-title">{title}</h1>
<div class="doc-tools">
  <button type="button" id="readBtn">&#128266; Read Aloud</button>
  <button type="button" id="stopBtn" disabled>&#9632; Stop</button>
  <button type="button" class="primary" id="pdfBtn">&#128196; Save as PDF</button>
  <span id="readStatus"></span>
</div>
<div id="docBody">{body_html}</div>
</div>
<script>
(function () {{
  var readBtn = document.getElementById('readBtn');
  var stopBtn = document.getElementById('stopBtn');
  var statusEl = document.getElementById('readStatus');

  document.getElementById('pdfBtn').addEventListener('click', function () {{
    // The browser's own print-to-PDF: perfect fidelity (fonts, tables, images,
    // unicode) with no server-side renderer to keep alive. The @media print
    // block above turns the dark page into a clean white document first.
    window.print();
  }});

  // ---- Read aloud -------------------------------------------------------
  // Split the rendered text into sentence-bounded chunks: /api/tts caps at
  // 3000 chars, and chunking is also what makes pause/stop feel instant
  // instead of waiting on one giant audio file.
  var CHUNK_MAX = 1200;
  function buildChunks() {{
    var raw = (document.getElementById('docBody').innerText || '').replace(/\\s+/g, ' ').trim();
    var full = {title_js} + '. ' + raw;
    var sentences = full.match(/[^.!?]+[.!?]*\\s*/g) || [full];
    var out = [], cur = '';
    sentences.forEach(function (s) {{
      if ((cur + s).length > CHUNK_MAX && cur) {{ out.push(cur.trim()); cur = ''; }}
      // A single sentence longer than the cap still has to go somewhere; the
      // endpoint truncates at 3000, so hard-split anything bigger than that.
      while (s.length > 2800) {{ out.push(s.slice(0, 2800)); s = s.slice(2800); }}
      cur += s;
    }});
    if (cur.trim()) out.push(cur.trim());
    return out.filter(Boolean);
  }}

  var chunks = [], idx = 0, audio = null, playing = false, paused = false, useBrowserVoice = false;

  function setUi(state) {{
    readBtn.innerHTML = state === 'playing' ? '&#10074;&#10074; Pause'
                      : state === 'paused' ? '&#9654; Resume'
                      : '&#128266; Read Aloud';
    stopBtn.disabled = state === 'idle';
    statusEl.textContent = state === 'idle' ? ''
      : 'Section ' + Math.min(idx + 1, chunks.length) + ' of ' + chunks.length;
  }}

  function stopAll() {{
    playing = false; paused = false; idx = 0;
    if (audio) {{ audio.pause(); audio = null; }}
    if (window.speechSynthesis) window.speechSynthesis.cancel();
    setUi('idle');
  }}

  // Fallback for anyone reading a shared link: /api/tts sits behind the login
  // gate, so a client opening this document gets a 401. The browser's built-in
  // voice keeps read-aloud working for them instead of failing silently.
  function speakInBrowser() {{
    if (!window.speechSynthesis) {{ statusEl.textContent = 'Read aloud is not supported in this browser.'; return; }}
    useBrowserVoice = true;
    window.speechSynthesis.cancel();
    var next = function () {{
      if (!playing || idx >= chunks.length) {{ stopAll(); return; }}
      var u = new SpeechSynthesisUtterance(chunks[idx]);
      u.rate = 1.0;
      u.onend = function () {{ if (playing) {{ idx++; setUi('playing'); next(); }} }};
      window.speechSynthesis.speak(u);
    }};
    setUi('playing');
    next();
  }}

  function playChunk() {{
    if (!playing || idx >= chunks.length) {{ if (playing) stopAll(); return; }}
    setUi('playing');
    fetch('/api/tts', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ text: chunks[idx] }})
    }}).then(function (r) {{
      if (!r.ok) throw new Error('tts ' + r.status);
      return r.blob();
    }}).then(function (blob) {{
      if (!playing) return;
      audio = new Audio(URL.createObjectURL(blob));
      audio.onended = function () {{ if (playing) {{ idx++; playChunk(); }} }};
      audio.onerror = function () {{ if (playing) {{ idx++; playChunk(); }} }};
      audio.play();
    }}).catch(function () {{
      if (playing) speakInBrowser();
    }});
  }}

  readBtn.addEventListener('click', function () {{
    if (!playing) {{
      chunks = buildChunks();
      if (!chunks.length) {{ statusEl.textContent = 'Nothing to read.'; return; }}
      playing = true; paused = false; idx = 0; useBrowserVoice = false;
      playChunk();
      return;
    }}
    if (paused) {{  // resume
      paused = false;
      if (useBrowserVoice) window.speechSynthesis.resume();
      else if (audio) audio.play();
      setUi('playing');
    }} else {{      // pause
      paused = true;
      if (useBrowserVoice) window.speechSynthesis.pause();
      else if (audio) audio.pause();
      setUi('paused');
    }}
  }});

  stopBtn.addEventListener('click', stopAll);
  window.addEventListener('beforeprint', function () {{ if (playing) stopAll(); }});
}})();
</script>
</body></html>"""
    return HTMLResponse(page)


# Serve the frontend
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    return FileResponse("static/index.html")
