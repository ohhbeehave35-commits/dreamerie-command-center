"""
Media asset library for the Command Center.

Lets the owner store photo/video links (hosted anywhere -- Dropbox, Google
Drive, Cloudinary, wherever) under a memorable name and TAGS, and lets
Annabelle look one up by name/tag when drafting a social post instead of
asking "what's the URL?" every single time. This is the piece that turns
draft_social_post's media_url requirement (see social.py) from "the owner
has to hunt for a link every time" into "Annabelle already knows where the
lavender candle photos are."

Same Airtable-table-per-feature pattern as crm.py/social.py: auto-created on
first use, graceful "not connected" if Airtable isn't configured.

Deliberately does NOT do any file upload/hosting itself -- the owner's asset
folder (Dropbox recommended: already in use, $0, direct-link friendly) is the
source of truth for the actual files. This table is just the searchable
index: name, URL, tags, media type, notes.
"""

import httpx

from . import crm

ASSETS_TABLE = "Media Assets"
MEDIA_TYPES = ["Photo", "Video", "Audio", "Other"]

_assets_table_id_cache = None


def _ensure_assets_table() -> str:
    """Return the Media Assets table id, creating it if needed."""
    global _assets_table_id_cache
    if _assets_table_id_cache:
        return _assets_table_id_cache
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{crm._API}/v0/meta/bases/{crm.AIRTABLE_BASE_ID}/tables", headers=crm._headers())
        r.raise_for_status()
        for t in r.json().get("tables", []):
            if t.get("name", "").lower() == ASSETS_TABLE.lower():
                _assets_table_id_cache = t["id"]
                return _assets_table_id_cache
        # Primary (first) field must be a plain text type in Airtable.
        fields = [
            {"name": "Name", "type": "singleLineText"},
            {"name": "URL", "type": "singleLineText"},
            {"name": "Type", "type": "singleSelect", "options": {"choices": [
                {"name": t} for t in MEDIA_TYPES]}},
            {"name": "Tags", "type": "singleLineText"},
            {"name": "Notes", "type": "multilineText"},
        ]
        r = c.post(f"{crm._API}/v0/meta/bases/{crm.AIRTABLE_BASE_ID}/tables",
                   headers=crm._headers(), json={"name": ASSETS_TABLE, "fields": fields})
        r.raise_for_status()
        _assets_table_id_cache = r.json()["id"]
        return _assets_table_id_cache


def add_asset_checked(name: str, url: str, media_type: str = "Photo",
                      tags: str = "", notes: str = "") -> tuple:
    """Register one asset. Returns (ok: bool, message: str).

    The string-only add_asset() below can't be checked by a caller -- every
    outcome is prose, so "saved" and "Airtable rejected it" are the same type.
    Anything that needs to KNOW whether the write landed (the owner-side
    /api/assets POST) calls this instead. Never raises.
    """
    if not crm.is_configured():
        return False, "The asset library isn't available (Airtable not connected)."
    if not name.strip() or not url.strip():
        return False, "I need both a name and a URL to save an asset."
    media_type = (media_type or "Photo").strip().title()
    if media_type not in MEDIA_TYPES:
        media_type = "Other"
    try:
        tid = _ensure_assets_table()
        with httpx.Client(timeout=30) as c:
            r = c.post(f"{crm._API}/v0/{crm.AIRTABLE_BASE_ID}/{tid}", headers=crm._headers(),
                       json={"fields": {
                           "Name": name.strip()[:200],
                           "URL": url.strip()[:1000],
                           "Type": media_type,
                           "Tags": tags.strip()[:500],
                           "Notes": notes.strip()[:2000],
                       }, "typecast": True})
            if r.status_code >= 400:
                body = (r.text or "")[:300]
                return False, f"The asset store rejected it (HTTP {r.status_code}): {body}"
        return True, f"Saved \"{name.strip()}\" ({media_type}) to the asset library."
    except Exception as e:
        return False, f"Couldn't save that asset: {type(e).__name__}: {e}"


def add_asset(name: str, url: str, media_type: str = "Photo", tags: str = "", notes: str = "") -> str:
    """Register one asset (a link to a photo/video hosted elsewhere) under a
    memorable name. Returns a confirmation or explanation -- never raises.

    Kept string-returning for the eight existing callers (the save_asset tool,
    the Dropbox/Drive importers, the image/video generators), some of which
    pattern-match the text. New code should call add_asset_checked().
    """
    return add_asset_checked(name, url, media_type, tags, notes)[1]


def list_assets_raw(limit: int = 20, media_type: str = "") -> list:
    """Return recent assets as a list of dicts for the media dock API.
    Each dict has: name, url, type, tags, notes, id.
    Returns [] on error or when Airtable is not configured."""
    if not crm.is_configured():
        return []
    try:
        tid = _ensure_assets_table()
        params = {
            "pageSize": str(max(1, min(limit, 50))),
            "sort[0][field]": "Name",
            "sort[0][direction]": "desc",
        }
        if media_type.strip():
            params["filterByFormula"] = "{Type}='" + media_type.strip().title().replace("'", "") + "'"
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{crm._API}/v0/{crm.AIRTABLE_BASE_ID}/{tid}", headers=crm._headers(), params=params)
            r.raise_for_status()
        out = []
        for rec in r.json().get("records", []):
            f = rec.get("fields", {})
            out.append({
                "id": rec.get("id", ""),
                "name": f.get("Name", ""),
                "url": f.get("URL", ""),
                "type": f.get("Type", "Photo"),
                "tags": f.get("Tags", ""),
                "notes": f.get("Notes", ""),
            })
        return out
    except Exception:
        return []


def find_assets(query: str = "", media_type: str = "", limit: int = 10) -> str:
    """Search the asset library by name/tag substring and/or type. Returns a
    short list (name, type, URL) or an explanation -- never raises."""
    if not crm.is_configured():
        return "The asset library isn't available (Airtable not connected)."
    try:
        tid = _ensure_assets_table()
        formula_parts = []
        if query.strip():
            q = query.strip().replace("'", "")
            formula_parts.append(
                "OR(FIND(LOWER('" + q + "'), LOWER({Name}))>0, "
                "FIND(LOWER('" + q + "'), LOWER({Tags}))>0)"
            )
        if media_type.strip():
            formula_parts.append("{Type}='" + media_type.strip().title().replace("'", "") + "'")
        params = {"pageSize": str(max(1, min(limit, 25)))}
        if formula_parts:
            params["filterByFormula"] = "AND(" + ",".join(formula_parts) + ")" if len(formula_parts) > 1 else formula_parts[0]
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{crm._API}/v0/{crm.AIRTABLE_BASE_ID}/{tid}", headers=crm._headers(), params=params)
            r.raise_for_status()
        recs = r.json().get("records", [])
        if not recs:
            return "No matching assets found in the library."
        lines = []
        for rec in recs:
            f = rec.get("fields", {})
            tag_str = f" [{f['Tags']}]" if f.get("Tags") else ""
            lines.append(f"{f.get('Name','?')} ({f.get('Type','?')}){tag_str}: {f.get('URL','')}")
        return "\n".join(lines)
    except Exception as e:
        return f"Couldn't search the asset library: {type(e).__name__}: {e}"
