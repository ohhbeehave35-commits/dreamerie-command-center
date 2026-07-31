"""
Dropbox file-access connector for the Command Center.

Lets Annabelle browse, search, and pull files out of Vinny's Dropbox so he
doesn't have to keep pasting share links. Started read-only; append_text_file/
upload_text_file/download_text_file (added for the Skills Log Dropbox backup)
are the first writes this connector makes -- they need the `files.content.write`
scope, which the original read-only setup did NOT request. If a write 401s with
a scope/permission error, that's why: re-grant the Dropbox app permissions
(Permissions tab -> check files.content.write -> Submit) and reconnect.

Two auth paths, either works:

    (A) Short-lived token (simplest, good for testing)
        Set DROPBOX_ACCESS_TOKEN in Render env. That's it. Expires after
        4 hours per Dropbox's default, so this is really only for smoke tests.

    (B) OAuth refresh flow (production)
        Set DROPBOX_APP_KEY + DROPBOX_APP_SECRET in Render env, then hit
        /dropbox/connect once to grant offline access. We store the refresh
        token in Airtable Settings and auto-refresh access tokens as needed.

Files pulled with `download` are pushed into the existing Asset Library
(assets.add_asset) with a temporary shared link so Annabelle can reference
them in social posts or emails.
"""

import os
import json
import time
import httpx
import logging

from . import crm

log = logging.getLogger(__name__)

# ── OAuth config ─────────────────────────────────────────────────────────────
APP_KEY = os.environ.get("DROPBOX_APP_KEY", "")
APP_SECRET = os.environ.get("DROPBOX_APP_SECRET", "")
def _default_redirect(path: str) -> str:
    """Production-safe default for an OAuth callback. A hard-coded
    http://127.0.0.1:8040/... default is a silent dead end on a deployed app:
    the provider redirects the browser to the USER'S OWN machine on a port
    nothing listens on, the grant never completes, and the integration reports
    "not configured" forever with no error anywhere. Derive from the deployed
    site instead. (Ported from Stinger 3f4be89.)"""
    base = os.environ.get("SITE_BASE_URL", "").rstrip("/")
    if not base:
        base = "https://dreamerie-command-center.onrender.com"
    return f"{base}{path}"


REDIRECT_URI = os.environ.get("DROPBOX_REDIRECT_URI") or _default_redirect("/dropbox/callback")

DROPBOX_TOKEN_KEY = "dropbox_oauth_token"  # settings key for stored refresh info
_STATIC_TOKEN = os.environ.get("DROPBOX_ACCESS_TOKEN", "").strip()

_API = "https://api.dropboxapi.com/2"
_CONTENT = "https://content.dropboxapi.com/2"
TIMEOUT = 15.0


# ── Configuration status ─────────────────────────────────────────────────────

def is_configured() -> bool:
    """True if EITHER a static token OR a stored OAuth token exists."""
    if _STATIC_TOKEN:
        return True
    return bool(crm.get_setting(DROPBOX_TOKEN_KEY, ""))


def has_oauth() -> bool:
    """True if OAuth flow can be run (app key + secret present)."""
    return bool(APP_KEY and APP_SECRET)


# ── Token handling ───────────────────────────────────────────────────────────

def _load_stored_token() -> dict | None:
    raw = crm.get_setting(DROPBOX_TOKEN_KEY, "")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _save_stored_token(data: dict) -> None:
    crm.set_setting(DROPBOX_TOKEN_KEY, json.dumps(data))


def _refresh_access_token(refresh_token: str) -> dict:
    """Exchange a refresh token for a fresh access token."""
    r = httpx.post(
        "https://api.dropbox.com/oauth2/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        auth=(APP_KEY, APP_SECRET),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()  # {access_token, expires_in, token_type, ...}


def _access_token() -> str:
    """Return a currently-valid access token, refreshing if needed.
    Raises if Dropbox isn't configured at all."""
    if _STATIC_TOKEN:
        return _STATIC_TOKEN

    stored = _load_stored_token()
    if not stored:
        raise RuntimeError("Dropbox is not connected — set DROPBOX_ACCESS_TOKEN or run OAuth at /dropbox/connect.")

    tok = stored.get("access_token", "")
    expires_at = stored.get("expires_at", 0)
    refresh = stored.get("refresh_token", "")

    # Refresh if the token is missing, expired, or expires within 60s
    if not tok or (expires_at and expires_at - 60 < time.time()):
        if not refresh or not has_oauth():
            raise RuntimeError("Dropbox token expired and no refresh token available — reconnect at /dropbox/connect.")
        fresh = _refresh_access_token(refresh)
        tok = fresh["access_token"]
        stored["access_token"] = tok
        stored["expires_at"] = time.time() + fresh.get("expires_in", 14400)
        _save_stored_token(stored)
    return tok


def store_oauth_result(payload: dict) -> None:
    """Called by /dropbox/callback after code-for-token exchange succeeds."""
    data = {
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token", ""),
        "expires_at": time.time() + payload.get("expires_in", 14400),
        "account_id": payload.get("account_id", ""),
        "connected_at": time.time(),
    }
    _save_stored_token(data)


# ── OAuth authorize URL ──────────────────────────────────────────────────────

def authorize_url() -> str | None:
    """Build the Dropbox authorize URL. Returns None if OAuth isn't configured."""
    if not has_oauth():
        return None
    return (
        "https://www.dropbox.com/oauth2/authorize"
        f"?client_id={APP_KEY}"
        "&response_type=code"
        "&token_access_type=offline"  # get refresh token
        f"&redirect_uri={REDIRECT_URI}"
    )


def exchange_code(code: str) -> dict:
    """Exchange an authorization code for an access + refresh token."""
    r = httpx.post(
        "https://api.dropbox.com/oauth2/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
        },
        auth=(APP_KEY, APP_SECRET),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


# ── API calls ────────────────────────────────────────────────────────────────

def _post(path: str, body: dict, content_api: bool = False) -> dict:
    base = _CONTENT if content_api else _API
    r = httpx.post(
        f"{base}{path}",
        headers={
            "Authorization": f"Bearer {_access_token()}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=TIMEOUT,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Dropbox {path} HTTP {r.status_code}: {r.text[:200]}")
    return r.json() if r.text else {}


def list_folder(path: str = "") -> list[dict]:
    """List files and folders at a Dropbox path. Path '' or '/' means root.
    Returns a list of {name, path, kind, size, modified}."""
    p = "" if path in ("", "/") else (path if path.startswith("/") else "/" + path)
    resp = _post("/files/list_folder", {"path": p, "recursive": False, "limit": 200})
    out = []
    for e in resp.get("entries", []):
        tag = e.get(".tag", "")
        out.append({
            "name": e.get("name", ""),
            "path": e.get("path_display", ""),
            "kind": "folder" if tag == "folder" else "file",
            "size": e.get("size"),
            "modified": e.get("client_modified") or e.get("server_modified"),
        })
    return out


def search(query: str, max_results: int = 25) -> list[dict]:
    """Search the entire Dropbox for files matching `query`. Filenames + content."""
    if not query.strip():
        return []
    resp = _post("/files/search_v2", {
        "query": query.strip(),
        "options": {"max_results": max(1, min(max_results, 100))},
    })
    out = []
    for match in resp.get("matches", []):
        meta = match.get("metadata", {}).get("metadata", {})
        tag = meta.get(".tag", "")
        out.append({
            "name": meta.get("name", ""),
            "path": meta.get("path_display", ""),
            "kind": "folder" if tag == "folder" else "file",
            "size": meta.get("size"),
            "modified": meta.get("client_modified") or meta.get("server_modified"),
        })
    return out


def get_shared_link(path: str) -> str:
    """Get (or create) a shared link for a Dropbox path. Returns a direct URL."""
    p = path if path.startswith("/") else "/" + path
    try:
        resp = _post("/sharing/create_shared_link_with_settings", {
            "path": p,
            "settings": {"requested_visibility": "public"},
        })
        url = resp.get("url", "")
    except RuntimeError as e:
        # Already exists → list_shared_links returns it
        if "shared_link_already_exists" not in str(e):
            raise
        resp = _post("/sharing/list_shared_links", {"path": p, "direct_only": True})
        links = resp.get("links", [])
        if not links:
            raise
        url = links[0].get("url", "")
    # Convert the ?dl=0 preview URL to ?dl=1 direct-download URL
    if url.endswith("?dl=0"):
        url = url[:-5] + "?dl=1"
    elif "?dl=0" in url:
        url = url.replace("?dl=0", "?dl=1")
    return url


def save_to_asset_library(path: str, name: str = "", tags: str = "") -> str:
    """Fetch a shared link for the file and register it in the Asset Library.
    Returns a human-readable confirmation string."""
    from . import assets
    try:
        url = get_shared_link(path)
    except Exception as e:
        return f"Couldn't get a shared link for {path}: {e}"
    display_name = name or path.rsplit("/", 1)[-1]
    lower = display_name.lower()
    if any(lower.endswith(ext) for ext in (".mp4", ".mov", ".webm", ".avi")):
        media_type = "Video"
    elif any(lower.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic")):
        media_type = "Photo"
    elif any(lower.endswith(ext) for ext in (".mp3", ".wav", ".m4a")):
        media_type = "Audio"
    else:
        media_type = "Other"
    result = assets.add_asset(display_name, url, media_type=media_type, tags=tags,
                              notes=f"Sourced from Dropbox: {path}")
    return f"Saved {display_name} to the Asset Library. URL: {url}. ({result})"


def download_text_file(path: str) -> tuple[bool, str]:
    """Download a UTF-8 text file's content. Returns (found, content_or_error).
    found=False + content='' (not an error string) means the file simply
    doesn't exist yet -- the normal case the first time something appends to
    a log that hasn't been created."""
    p = path if path.startswith("/") else "/" + path
    try:
        r = httpx.post(
            f"{_CONTENT}/files/download",
            headers={
                "Authorization": f"Bearer {_access_token()}",
                "Dropbox-API-Arg": json.dumps({"path": p}),
            },
            timeout=TIMEOUT,
        )
        if r.status_code == 409:
            return False, ""  # path/not_found -- file doesn't exist yet
        if r.status_code >= 400:
            return False, f"Dropbox download HTTP {r.status_code}: {r.text[:200]}"
        return True, r.content.decode("utf-8", errors="replace")
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def upload_text_file(path: str, content: str) -> tuple[bool, str]:
    """Upload/overwrite a UTF-8 text file at `path`. Returns (ok, error_or_empty).
    Requires the files.content.write scope -- see module docstring."""
    p = path if path.startswith("/") else "/" + path
    try:
        r = httpx.post(
            f"{_CONTENT}/files/upload",
            headers={
                "Authorization": f"Bearer {_access_token()}",
                "Dropbox-API-Arg": json.dumps({
                    "path": p, "mode": "overwrite", "autorename": False, "mute": True,
                }),
                "Content-Type": "application/octet-stream",
            },
            content=content.encode("utf-8"),
            timeout=TIMEOUT,
        )
        if r.status_code >= 400:
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def append_text_file(path: str, entry: str) -> tuple[bool, str]:
    """Append `entry` to a text file, creating it if it doesn't exist yet.
    Read-modify-write (Dropbox has no native append) -- fine for a low-volume
    notes log, not safe for high-concurrency writers. Returns (ok, error)."""
    found, existing_or_err = download_text_file(path)
    if not found and existing_or_err:
        return False, existing_or_err  # a real error, not just "doesn't exist"
    existing = existing_or_err if found else ""
    return upload_text_file(path, existing + entry)


def probe_connection() -> tuple[bool, str | None]:
    """Cheap authenticated call to prove the token works. Returns (ok, error)."""
    try:
        _post("/users/get_current_account", {})
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:200]
