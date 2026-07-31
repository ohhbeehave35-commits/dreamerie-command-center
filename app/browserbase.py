"""
Browserbase session management -- the foundation for "the hands".

Validated live 30 Jul 2026 on the free tier: a real cloud Chrome session was
created (RUNNING) and released (COMPLETED) via this exact API surface.
Replay: browserbase.com/sessions/0e3cceb7-c908-4b05-9a03-ba45af71b3d4

WHAT THIS DELIBERATELY IS NOT YET: an agent tool. This module manages
sessions -- create, release, list, replay links. It cannot CLICK anything;
driving the browser (navigate, act, extract) needs a CDP client layered on
top, and that is a separate build. Until that layer exists, NO tool is
offered to the model -- offering "browser automation" that can only open and
close an empty browser is the advertised-vs-implemented gap this codebase
has a detector for. The tool ships WITH the driving layer, not before it.

Cost model this respects (free tier): sessions bill by browser-minute, the
default timeout is 5 minutes, and an un-released session idles the meter.
So release_session() exists and every future caller is expected to use
try/finally around it.

Config: BROWSERBASE_API_KEY only. The project id is resolved from the key
(and cached) -- never asked for, never configured separately.
"""

import logging
import os

import httpx

log = logging.getLogger(__name__)

_API = "https://api.browserbase.com/v1"
_project_id_cache = None


def is_configured() -> bool:
    return bool(os.environ.get("BROWSERBASE_API_KEY", "").strip())


def _headers() -> dict:
    return {"X-BB-API-Key": os.environ["BROWSERBASE_API_KEY"].strip(),
            "Content-Type": "application/json"}


def _project_id() -> str:
    """Resolve (and cache) the project id from the API key. '' on failure."""
    global _project_id_cache
    if _project_id_cache:
        return _project_id_cache
    try:
        r = httpx.get(f"{_API}/projects", headers=_headers(), timeout=30)
        r.raise_for_status()
        rows = r.json()
        if isinstance(rows, list) and rows:
            _project_id_cache = rows[0].get("id", "")
            return _project_id_cache
    except Exception as e:
        log.warning("BROWSERBASE_PROJECT_RESOLVE_FAIL %s", type(e).__name__)
    return ""


def create_session() -> tuple:
    """(ok, info). info on success: {id, connect_url, replay_url, expires_at}.
    On failure: a plain-language reason. Never raises."""
    if not is_configured():
        return False, "browser sessions aren't connected (no Browserbase key set)"
    pid = _project_id()
    if not pid:
        return False, "couldn't resolve the Browserbase project from the key"
    try:
        r = httpx.post(f"{_API}/sessions", headers=_headers(),
                       json={"projectId": pid}, timeout=45)
        if r.status_code == 402:
            return False, ("the Browserbase free allowance is used up -- browser "
                           "sessions will work again when it resets or the plan "
                           "is upgraded")
        if r.status_code >= 400:
            return False, f"Browserbase returned HTTP {r.status_code}"
        d = r.json()
        return True, {
            "id": d.get("id", ""),
            "connect_url": d.get("connectUrl", ""),
            "replay_url": f"https://www.browserbase.com/sessions/{d.get('id', '')}",
            "expires_at": d.get("expiresAt", ""),
        }
    except Exception as e:
        return False, f"couldn't start a browser session ({type(e).__name__})"


def release_session(session_id: str) -> tuple:
    """(ok, message). Ends a session so it stops billing minutes. Never raises."""
    if not is_configured():
        return False, "not connected"
    sid = (session_id or "").strip()
    if not sid:
        return False, "no session id"
    try:
        r = httpx.post(f"{_API}/sessions/{sid}", headers=_headers(),
                       json={"projectId": _project_id(),
                             "status": "REQUEST_RELEASE"}, timeout=30)
        if r.status_code >= 400:
            return False, f"release returned HTTP {r.status_code}"
        return True, "released"
    except Exception as e:
        return False, f"release failed ({type(e).__name__})"


def list_sessions(limit: int = 10) -> list:
    """Recent sessions as [{id, status, replay_url}]. [] on any failure."""
    if not is_configured():
        return []
    try:
        r = httpx.get(f"{_API}/sessions", headers=_headers(), timeout=30)
        r.raise_for_status()
        out = []
        for d in (r.json() or [])[:limit]:
            out.append({"id": d.get("id", ""), "status": d.get("status", ""),
                        "replay_url": f"https://www.browserbase.com/sessions/{d.get('id', '')}"})
        return out
    except Exception:
        return []
