"""
Google Drive file-access connector for the Command Center.

Read-only Drive access for Annabelle. Reuses the same GOOGLE_CLIENT_ID /
GOOGLE_CLIENT_SECRET already registered for Calendar/Gmail, but runs its own
OAuth flow at /drive/connect and stores its own token under a separate
settings key so it can be connected/revoked independently of Calendar.

Files pulled with `download` are pushed into the Asset Library with a
shareable web-view link — the actual file bytes stay in Drive.
"""

import os
import json
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from . import crm

CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get(
    "GOOGLE_DRIVE_REDIRECT_URI",
    "http://127.0.0.1:8040/auth/drive-callback",
)
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

DRIVE_TOKEN_KEY = "google_drive_oauth_token"


# ── Config + OAuth flow ──────────────────────────────────────────────────────

def is_configured() -> bool:
    return bool(crm.get_setting(DRIVE_TOKEN_KEY, ""))


def has_oauth() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET)


def get_oauth_flow():
    if not has_oauth():
        return None
    return Flow.from_client_config(
        {
            "web": {
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [REDIRECT_URI],
            }
        },
        scopes=SCOPES,
        autogenerate_code_verifier=False,
    )


def store_token(token_json: str) -> None:
    crm.set_setting(DRIVE_TOKEN_KEY, token_json)


def _get_credentials() -> Optional[Credentials]:
    raw = crm.get_setting(DRIVE_TOKEN_KEY, "")
    if not raw:
        return None
    try:
        creds = Credentials.from_authorized_user_info(json.loads(raw), SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            store_token(creds.to_json())
        return creds
    except Exception:
        return None


def _service():
    creds = _get_credentials()
    if not creds:
        return None
    try:
        return build("drive", "v3", credentials=creds, cache_discovery=False)
    except Exception:
        return None


# ── API surface ──────────────────────────────────────────────────────────────

def list_files(folder_id: str = "", page_size: int = 50) -> list[dict]:
    """List files in Drive root (or under a folder). Returns compact dicts."""
    svc = _service()
    if not svc:
        raise RuntimeError("Google Drive is not connected — visit /drive/connect first.")
    q_parts = ["trashed = false"]
    if folder_id:
        q_parts.append(f"'{folder_id}' in parents")
    else:
        q_parts.append("'root' in parents")
    resp = svc.files().list(
        q=" and ".join(q_parts),
        pageSize=max(1, min(page_size, 200)),
        fields="files(id,name,mimeType,size,modifiedTime,webViewLink,parents)",
        orderBy="modifiedTime desc",
    ).execute()
    return [_shape(f) for f in resp.get("files", [])]


def search(query: str, page_size: int = 25) -> list[dict]:
    """Search Drive by name or full-text content."""
    svc = _service()
    if not svc:
        raise RuntimeError("Google Drive is not connected — visit /drive/connect first.")
    q = query.replace("'", "\\'").strip()
    if not q:
        return []
    # `fullText contains` covers content; `name contains` covers filenames.
    drive_q = f"trashed = false and (name contains '{q}' or fullText contains '{q}')"
    resp = svc.files().list(
        q=drive_q,
        pageSize=max(1, min(page_size, 100)),
        fields="files(id,name,mimeType,size,modifiedTime,webViewLink,parents)",
        orderBy="modifiedTime desc",
    ).execute()
    return [_shape(f) for f in resp.get("files", [])]


def _shape(f: dict) -> dict:
    return {
        "id": f.get("id", ""),
        "name": f.get("name", ""),
        "mime": f.get("mimeType", ""),
        "kind": "folder" if f.get("mimeType") == "application/vnd.google-apps.folder" else "file",
        "size": int(f["size"]) if f.get("size") else None,
        "modified": f.get("modifiedTime"),
        "url": f.get("webViewLink", ""),
    }


def get_file_link(file_id: str) -> str:
    """Return the webViewLink for a Drive file id."""
    svc = _service()
    if not svc:
        raise RuntimeError("Google Drive is not connected — visit /drive/connect first.")
    f = svc.files().get(fileId=file_id, fields="id,name,webViewLink").execute()
    return f.get("webViewLink", "")


def save_to_asset_library(file_id: str, name: str = "", tags: str = "") -> str:
    """Register a Drive file in the Asset Library by its webViewLink."""
    from . import assets
    svc = _service()
    if not svc:
        return "Google Drive is not connected — visit /drive/connect first."
    try:
        f = svc.files().get(fileId=file_id,
                            fields="id,name,mimeType,webViewLink").execute()
    except Exception as e:
        return f"Couldn't fetch that Drive file: {e}"
    display_name = name or f.get("name", "Untitled")
    url = f.get("webViewLink", "")
    mime = f.get("mimeType", "")
    if mime.startswith("video/"):
        media_type = "Video"
    elif mime.startswith("image/"):
        media_type = "Photo"
    elif mime.startswith("audio/"):
        media_type = "Audio"
    else:
        media_type = "Other"
    result = assets.add_asset(display_name, url, media_type=media_type, tags=tags,
                              notes=f"Sourced from Google Drive: id={file_id}")
    return f"Saved {display_name} to the Asset Library. URL: {url}. ({result})"


def probe_connection() -> tuple[bool, str | None]:
    """Cheap authenticated call to prove the token works."""
    svc = _service()
    if not svc:
        return False, "Not connected"
    try:
        svc.about().get(fields="user(displayName,emailAddress)").execute()
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:200]
