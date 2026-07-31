"""Remote support recorder -- what the app is doing, visible without a human
middleman.

Why this exists: on 28 Jul 2026 the only way to learn that Susan's app was
failing was Susan describing her screen to someone who described it to me. Her
errors landed in Render's log stream, which nobody was watching, and the two
things that actually explained most of her afternoon -- "the app restarted 40
seconds ago" and "the unlock form is rejecting codes" -- were invisible from
outside.

This module keeps a small in-memory ring buffer of the things that went WRONG
(and only those), plus a boot timestamp, and serves them to an owner-only
support page. No database, no Airtable writes, no cost. It resets on restart,
which is not a limitation to apologise for: a cleared buffer plus a fresh
`booted_at` IS the signal that the app restarted, which is exactly what was
being misread as "the server is broken".

WHAT IS DELIBERATELY NOT RECORDED
    Request and response BODIES. Query strings. Headers. Cookies. Chat text.
    Lead details. Anything Susan typed.
This is a support view, not a wiretap. It answers "is it broken, where, and
since when" -- never "what is she working on". Everything stored here is
scrubbed through _scrub() before it is kept, so a traceback that happens to
carry a key does not become a key sitting in memory.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections import Counter, deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# How many events to keep. Small on purpose -- this is a "what just happened"
# window, not an archive. ~200 covers a very bad hour.
MAX_EVENTS = int(os.environ.get("SUPPORT_MAX_EVENTS", "200"))

# A request slower than this is worth seeing even when it succeeded: on the
# free tier a 20s response is a cold start, and a cold start is what the user
# experiences as "trouble reaching server".
SLOW_MS = int(os.environ.get("SUPPORT_SLOW_MS", "6000"))

# Caps on anything a browser can send us. The client beacon is reachable
# without the gate (see main.py) precisely so a locked-out user can still be
# seen, which means these strings are untrusted input.
MAX_FIELD = 400
MAX_CLIENT_EVENTS_PER_IP = 30
CLIENT_WINDOW_SECONDS = 300

_lock = threading.Lock()
_events: deque = deque(maxlen=MAX_EVENTS)
_counts: Counter = Counter()
_client_hits: Dict[str, List[float]] = {}

BOOTED_AT = datetime.now(timezone.utc)
_BOOT_PERF = time.perf_counter()


# ---- redaction --------------------------------------------------------------

# Patterns for secrets that can appear inside an exception string. The env-var
# sweep below is the real guarantee; these catch shapes we may not have in env
# (e.g. a key echoed by an upstream API's error body).
_SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "sk-***"),
    (re.compile(r"pat[A-Za-z0-9]{6,}\.[A-Za-z0-9]{8,}"), "pat***"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"), "Bearer ***"),
    (re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd|access[_-]?code)"
                r"\s*[:=]\s*[\"']?([A-Za-z0-9._\-]{4,})"), r"\1=***"),
]

# Env vars whose VALUES must never appear in the buffer, matched by name.
_SECRET_ENV_HINT = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CODE|PAT|SID|WEBHOOK|DSN)")


def _secret_values() -> List[str]:
    """Every environment value that looks like a credential, longest first.

    Longest first matters: if two secrets share a prefix, replacing the short
    one first would leave the tail of the long one exposed.
    """
    vals = []
    for k, v in os.environ.items():
        if not v or len(v) < 8:
            continue
        if _SECRET_ENV_HINT.search(k.upper()):
            vals.append(v)
    vals.sort(key=len, reverse=True)
    return vals


def _scrub(text: Any, limit: int = MAX_FIELD) -> str:
    """Strip credentials out of a string and cap its length.

    Applied to EVERYTHING before it enters the buffer, including strings that
    "obviously" cannot contain a secret. The one that leaks is always the one
    nobody thought to check.
    """
    if text is None:
        return ""
    s = str(text)
    for v in _secret_values():
        if v in s:
            s = s.replace(v, "***")
    for pattern, repl in _SECRET_PATTERNS:
        s = pattern.sub(repl, s)
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    return s


def _clean_path(path: str) -> str:
    """Path only -- the query string is dropped, never stored.

    Query strings carry search terms, ids and, on some routes, tokens. None of
    that is needed to answer "which endpoint is failing".
    """
    return _scrub((path or "").split("?", 1)[0], limit=200)


# ---- recording --------------------------------------------------------------

def _push(event: Dict[str, Any]) -> None:
    event["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _lock:
        _events.append(event)
        _counts[event["kind"]] += 1


def record_request(method: str, path: str, status: int, duration_ms: int,
                   error: Optional[str] = None) -> None:
    """Record a request that failed or was unusually slow. Successes are not
    recorded -- a log of everything is a log nobody reads, and it would turn
    this into a record of her activity rather than of the app's faults."""
    clean = _clean_path(path)
    # The keep-alive ping and static assets are noise unless they actually broke.
    if status < 400 and duration_ms < SLOW_MS:
        return
    if clean == "/healthz" and status < 400:
        return
    # Every browser asks for these; a 404 on them is wallpaper, not a fault.
    if status == 404 and clean in ("/favicon.ico", "/apple-touch-icon.png",
                                   "/apple-touch-icon-precomposed.png"):
        return
    # A 401 on a PAGE route is the access gate answering an unauthenticated
    # visit -- the keep-warm ping, an uptime monitor's HEAD probe, a logged-out
    # tab. That is the gate WORKING, not a fault (it surfaced as a permanent
    # "1 recent fault -- HEAD / 401" toast on the flagship, 31 Jul). A 401 on
    # an /api/ path stays recorded: that is a session breaking mid-use, which
    # is exactly what this buffer exists to catch.
    if status == 401 and not clean.startswith("/api"):
        return
    kind = "server_error" if status >= 500 else ("client_error" if status >= 400 else "slow")
    _push({
        "kind": kind,
        "source": "server",
        "method": _scrub(method, 10),
        "path": clean,
        "status": status,
        "duration_ms": duration_ms,
        "detail": _scrub(error) if error else "",
    })


def record_note(kind: str, detail: str, path: str = "") -> None:
    """Record a non-HTTP event worth seeing: a failed unlock, a background job
    that died, a deliberate restart marker."""
    _push({
        "kind": _scrub(kind, 40),
        "source": "server",
        "method": "",
        "path": _clean_path(path),
        "status": None,
        "duration_ms": None,
        "detail": _scrub(detail),
    })


def _client_rate_ok(ip: str) -> bool:
    now = time.time()
    with _lock:
        hits = [t for t in _client_hits.get(ip, []) if now - t < CLIENT_WINDOW_SECONDS]
        if len(hits) >= MAX_CLIENT_EVENTS_PER_IP:
            _client_hits[ip] = hits
            return False
        hits.append(now)
        _client_hits[ip] = hits
        # Keep the map from growing without bound on a long uptime.
        if len(_client_hits) > 200:
            for k in [k for k, v in _client_hits.items()
                      if not v or now - v[-1] > CLIENT_WINDOW_SECONDS]:
                _client_hits.pop(k, None)
    return True


def record_client_event(payload: Dict[str, Any], ip: str, authenticated: bool) -> bool:
    """Record something that broke in HER BROWSER.

    This is the half the server has never been able to see. "Trouble reaching
    server" is a client-side event: the request timed out or never resolved, so
    by definition no server log line exists for it. Returns False when the
    reporter is rate-limited.
    """
    if not _client_rate_ok(ip):
        return False
    kind = str(payload.get("kind") or "client_error")
    if kind not in ("client_error", "client_network", "client_note"):
        kind = "client_error"
    _push({
        "kind": kind,
        "source": "browser" if authenticated else "browser (not signed in)",
        "method": _scrub(payload.get("method") or "", 10),
        "path": _clean_path(str(payload.get("path") or "")),
        "status": payload.get("status") if isinstance(payload.get("status"), int) else None,
        "duration_ms": None,
        "detail": _scrub(payload.get("detail") or ""),
        "ua": _scrub(payload.get("ua") or "", 160),
    })
    return True


# ---- reporting --------------------------------------------------------------

def _uptime_seconds() -> int:
    return int(time.perf_counter() - _BOOT_PERF)


def _human_uptime(seconds: int) -> str:
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


def report(limit: int = MAX_EVENTS) -> Dict[str, Any]:
    """Everything the support page shows, in one call.

    Deliberately does NO network I/O: it must answer instantly even when the
    thing that is broken is the network. The deep integration probe already
    exists at /api/diagnostic and the page fetches it separately, on demand.
    """
    with _lock:
        recent = list(_events)[-limit:][::-1]
        counts = dict(_counts)

    up = _uptime_seconds()
    # A restart inside the last few minutes explains almost every "it broke and
    # then it worked" report, so it is stated up front rather than inferred.
    restarted_recently = up < 300

    return {
        "rev": (os.environ.get("RENDER_GIT_COMMIT") or "unknown")[:12],
        "booted_at": BOOTED_AT.isoformat(timespec="seconds"),
        "uptime_seconds": up,
        "uptime_human": _human_uptime(up),
        "restarted_recently": restarted_recently,
        "now": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "free_tier_sleeps": os.environ.get("RENDER_SERVICE_ID") is not None,
        "counts": counts,
        "events_kept": len(recent),
        "buffer_limit": MAX_EVENTS,
        "events": recent,
        "integrations": _integration_flags(),
    }


# Which integrations are switched ON, by env var alone. Names only, never
# values -- this answers "is Twilio wired up on her deployment" without going
# near the credential itself.
_INTEGRATION_ENV = {
    "Anthropic (Claude)": ["ANTHROPIC_API_KEY"],
    "Airtable (memory)": ["AIRTABLE_TOKEN", "AIRTABLE_API_KEY"],
    "Airtable base": ["AIRTABLE_BASE_ID"],
    "Access code set": ["ACCESS_CODE"],
    "OpenAI (images)": ["OPENAI_API_KEY"],
    "ElevenLabs (voice)": ["ELEVEN_API_KEY", "ELEVENLABS_API_KEY"],
    "xAI (media)": ["XAI_API_KEY"],
    "Stripe": ["STRIPE_SECRET_KEY"],
    "Twilio SMS": ["TWILIO_ACCOUNT_SID"],
    "HubSpot": ["HUBSPOT_ACCESS_TOKEN"],
    "Push notifications": ["VAPID_PRIVATE_KEY"],
    "Dropbox": ["DROPBOX_ACCESS_TOKEN", "DROPBOX_APP_KEY"],
}


def _integration_flags() -> List[Dict[str, Any]]:
    out = []
    for label, keys in _INTEGRATION_ENV.items():
        out.append({"name": label, "on": any(os.environ.get(k) for k in keys)})
    return out


def reset_for_tests() -> None:
    """Clear the buffer. Tests only."""
    with _lock:
        _events.clear()
        _counts.clear()
        _client_hits.clear()
