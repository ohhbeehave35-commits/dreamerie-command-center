"""
Airtable-backed CRM for the Command Center.

Annabelle uses this to LOG leads/customers and LOOK THEM UP by voice, giving her
durable memory of the business (leads survive forever and sync across devices).

Configure with two env vars:
    AIRTABLE_TOKEN     - personal access token (starts with "pat...")
    AIRTABLE_BASE_ID   - the base id (starts with "app...")

If they're not set, the CRM is simply "not connected" and the tools say so
instead of crashing.
"""

import os
from datetime import datetime, timezone

import httpx

AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN", "")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID", "")
TABLE_NAME = os.environ.get("AIRTABLE_TABLE", "Leads")

_API = "https://api.airtable.com"
_table_id_cache = None  # resolved/created Leads table id
_conv_table_id_cache = None  # resolved/created Conversations table id
CONV_TABLE = "Conversations"
_build_table_id_cache = None  # resolved/created Build Requests table id
BUILD_TABLE = "Build Requests"
_settings_table_id_cache = None  # resolved/created Settings table id
SETTINGS_TABLE = "Settings"


def is_configured() -> bool:
    return bool(AIRTABLE_TOKEN and AIRTABLE_BASE_ID)


def _headers() -> dict:
    return {"Authorization": f"Bearer {AIRTABLE_TOKEN}", "Content-Type": "application/json"}


# The Leads table schema Annabelle writes to.
_FIELDS = [
    {"name": "Name", "type": "singleLineText"},
    {"name": "Phone", "type": "singleLineText"},
    {"name": "Email", "type": "email"},
    {"name": "Business", "type": "singleSelect", "options": {"choices": [
        {"name": "Ohh Beehave"}, {"name": "Stinger Industries"},
        {"name": "Late Nite Labs"}, {"name": "Other"}]}},
    {"name": "Request", "type": "multilineText"},
    {"name": "Status", "type": "singleSelect", "options": {"choices": [
        {"name": "New"}, {"name": "Contacted"}, {"name": "Quoted"},
        {"name": "Scheduled"}, {"name": "Done"}, {"name": "Lost"}]}},
    {"name": "Source", "type": "singleSelect", "options": {"choices": [
        {"name": "Call"}, {"name": "Text"}, {"name": "Website"},
        {"name": "Referral"}, {"name": "Walk-in"}, {"name": "Other"}]}},
    {"name": "Notes", "type": "multilineText"},
    {"name": "SMS Opt-In", "type": "checkbox", "options": {"icon": "check", "color": "greenBright"}},
]


def _ensure_table() -> str:
    """Return the Leads table id, creating the table if it doesn't exist yet."""
    global _table_id_cache
    if _table_id_cache:
        return _table_id_cache
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{_API}/v0/meta/bases/{AIRTABLE_BASE_ID}/tables", headers=_headers())
        r.raise_for_status()
        for t in r.json().get("tables", []):
            if t.get("name", "").lower() == TABLE_NAME.lower():
                _table_id_cache = t["id"]
                return _table_id_cache
        # Not found -> create it.
        r = c.post(
            f"{_API}/v0/meta/bases/{AIRTABLE_BASE_ID}/tables",
            headers=_headers(),
            json={"name": TABLE_NAME, "fields": _FIELDS},
        )
        r.raise_for_status()
        _table_id_cache = r.json()["id"]
        return _table_id_cache


def _ensure_conv_table() -> str:
    """Return the Conversations table id, creating it if needed."""
    global _conv_table_id_cache
    if _conv_table_id_cache:
        return _conv_table_id_cache
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{_API}/v0/meta/bases/{AIRTABLE_BASE_ID}/tables", headers=_headers())
        r.raise_for_status()
        for t in r.json().get("tables", []):
            if t.get("name", "").lower() == CONV_TABLE.lower():
                _conv_table_id_cache = t["id"]
                return _conv_table_id_cache
        # Primary (first) field must be a plain text type in Airtable, so Role
        # is singleLineText (values "user"/"assistant"), not a single-select.
        fields = [
            {"name": "Role", "type": "singleLineText"},
            {"name": "Content", "type": "multilineText"},
        ]
        r = c.post(f"{_API}/v0/meta/bases/{AIRTABLE_BASE_ID}/tables",
                   headers=_headers(), json={"name": CONV_TABLE, "fields": fields})
        r.raise_for_status()
        _conv_table_id_cache = r.json()["id"]
        return _conv_table_id_cache


def save_turn(role: str, content: str) -> None:
    """Persist one message (user or assistant) to Airtable. Silent on failure."""
    if not is_configured() or not content:
        return
    try:
        tid = _ensure_conv_table()
        with httpx.Client(timeout=30) as c:
            c.post(f"{_API}/v0/{AIRTABLE_BASE_ID}/{tid}", headers=_headers(),
                   json={"fields": {"Role": role, "Content": content[:100000]}, "typecast": True})
    except Exception:
        pass


def get_history(limit: int = 40) -> list:
    """Return the last `limit` messages as [{role, content}], oldest first."""
    if not is_configured():
        return []
    try:
        tid = _ensure_conv_table()
        # Pull recent records; sort by Airtable's built-in createdTime client-side.
        params = {"pageSize": str(min(int(limit or 40), 100))}
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{_API}/v0/{AIRTABLE_BASE_ID}/{tid}", headers=_headers(), params=params)
            r.raise_for_status()
        recs = r.json().get("records", [])
        recs.sort(key=lambda x: x.get("createdTime", ""))
        recs = recs[-int(limit or 40):]
        return [{"role": rec["fields"].get("Role", "user"),
                 "content": rec["fields"].get("Content", "")} for rec in recs if rec.get("fields", {}).get("Content")]
    except Exception:
        return []


def _ensure_build_table() -> str:
    """Return the Build Requests table id, creating it if needed."""
    global _build_table_id_cache
    if _build_table_id_cache:
        return _build_table_id_cache
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{_API}/v0/meta/bases/{AIRTABLE_BASE_ID}/tables", headers=_headers())
        r.raise_for_status()
        for t in r.json().get("tables", []):
            if t.get("name", "").lower() == BUILD_TABLE.lower():
                _build_table_id_cache = t["id"]
                return _build_table_id_cache
        fields = [
            {"name": "Request", "type": "singleLineText"},     # primary: plain text
            {"name": "Details", "type": "multilineText"},
            {"name": "Status", "type": "singleSelect", "options": {"choices": [
                {"name": "New"}, {"name": "Building"}, {"name": "Done"}]}},
        ]
        r = c.post(f"{_API}/v0/meta/bases/{AIRTABLE_BASE_ID}/tables",
                   headers=_headers(), json={"name": BUILD_TABLE, "fields": fields})
        r.raise_for_status()
        _build_table_id_cache = r.json()["id"]
        return _build_table_id_cache


def create_build_request(request="", details="") -> str:
    """Queue a capability/feature Annabelle is missing, for the dev team to build."""
    if not request:
        return "I need a short description of what to build."
    if not is_configured():
        return f"Noted this to build: {request}. (The build queue isn't connected yet.)"
    try:
        tid = _ensure_build_table()
        with httpx.Client(timeout=30) as c:
            c.post(f"{_API}/v0/{AIRTABLE_BASE_ID}/{tid}", headers=_headers(),
                   json={"fields": {"Request": request[:500], "Details": details, "Status": "New"}, "typecast": True})
        return f"Logged a build request for the dev team: {request}"
    except Exception as e:
        return f"I couldn't log that build request ({type(e).__name__}), but I've noted it: {request}"


def _ensure_settings_table() -> str:
    """Return the Settings table id, creating it if needed."""
    global _settings_table_id_cache
    if _settings_table_id_cache:
        return _settings_table_id_cache
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{_API}/v0/meta/bases/{AIRTABLE_BASE_ID}/tables", headers=_headers())
        r.raise_for_status()
        for t in r.json().get("tables", []):
            if t.get("name", "").lower() == SETTINGS_TABLE.lower():
                _settings_table_id_cache = t["id"]
                return _settings_table_id_cache
        fields = [
            {"name": "Key", "type": "singleLineText"},   # primary: plain text
            {"name": "Value", "type": "multilineText"},
        ]
        r = c.post(f"{_API}/v0/meta/bases/{AIRTABLE_BASE_ID}/tables",
                   headers=_headers(), json={"name": SETTINGS_TABLE, "fields": fields})
        r.raise_for_status()
        _settings_table_id_cache = r.json()["id"]
        return _settings_table_id_cache


# Short-TTL snapshot cache for the Settings table. Measured on the flagship
# instance: per-key reads stacked ~2.5s of Airtable round-trips BEFORE the
# model even started (caps, usage counts, voice, gmail are all Settings
# keys). One list call now fetches every key at once and is reused for TTL
# seconds; writes update the cache in place so this instance always sees its
# own changes immediately. Admin edits from the Settings panel still take
# effect within TTL seconds -- the zero-redeploy promise holds.
_SETTINGS_TTL = float(os.environ.get("SETTINGS_CACHE_TTL", "30"))
_settings_cache: dict = {}
_settings_cache_at: float = 0.0


def _settings_snapshot() -> dict:
    """Return {Key: Value} for the whole Settings table, cached for TTL secs."""
    global _settings_cache, _settings_cache_at
    import time as _time
    now = _time.time()
    if _settings_cache_at and (now - _settings_cache_at) < _SETTINGS_TTL:
        return _settings_cache
    tid = _ensure_settings_table()
    data: dict = {}
    offset = None
    with httpx.Client(timeout=30) as c:
        while True:
            params = {"pageSize": "100"}
            if offset:
                params["offset"] = offset
            r = c.get(f"{_API}/v0/{AIRTABLE_BASE_ID}/{tid}", headers=_headers(), params=params)
            r.raise_for_status()
            j = r.json()
            for rec in j.get("records", []):
                f = rec.get("fields", {})
                if f.get("Key"):
                    data[f["Key"]] = f.get("Value", "")
            offset = j.get("offset")
            if not offset:
                break
    _settings_cache, _settings_cache_at = data, now
    return data


def get_setting(key: str, default: str = "") -> str:
    """Read a single named setting (via the snapshot cache)."""
    if not is_configured():
        return default
    try:
        return _settings_snapshot().get(key, default)
    except Exception:
        return default


def set_setting(key: str, value: str) -> bool:
    """Write (create or update) a single named setting. Returns success.
    Updates the snapshot cache in place so this instance reads its own write
    immediately, without waiting out the TTL."""
    if not is_configured():
        return False
    try:
        tid = _ensure_settings_table()
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{_API}/v0/{AIRTABLE_BASE_ID}/{tid}", headers=_headers(),
                      params={"filterByFormula": "{Key}='" + key.replace("'", "") + "'", "pageSize": "1"})
            r.raise_for_status()
            recs = r.json().get("records", [])
            if recs:
                c.patch(f"{_API}/v0/{AIRTABLE_BASE_ID}/{tid}/{recs[0]['id']}",
                        headers=_headers(), json={"fields": {"Value": value}})
            else:
                c.post(f"{_API}/v0/{AIRTABLE_BASE_ID}/{tid}", headers=_headers(),
                       json={"fields": {"Key": key, "Value": value}, "typecast": True})
        if _settings_cache_at:
            _settings_cache[key] = str(value)
        return True
    except Exception:
        return False


def _search_usage_key() -> str:
    """Monthly bucket key, e.g. 'search_count_2026-07', so the cap resets each month."""
    return "search_count_" + datetime.now(timezone.utc).strftime("%Y-%m")


def get_search_count() -> int:
    """How many web searches have been used this calendar month."""
    try:
        return int(get_setting(_search_usage_key(), "0") or 0)
    except ValueError:
        return 0


def increment_search_count(n: int = 1) -> int:
    """Add `n` to this month's search count and return the new total."""
    total = get_search_count() + n
    set_setting(_search_usage_key(), str(total))
    return total


def _chat_usage_key(persona: str) -> str:
    """Monthly bucket key per persona, e.g. 'chat_count_public_2026-07'."""
    return f"chat_count_{persona}_" + datetime.now(timezone.utc).strftime("%Y-%m")


def get_chat_count(persona: str) -> int:
    """How many chat turns this persona has used this calendar month."""
    try:
        return int(get_setting(_chat_usage_key(persona), "0") or 0)
    except ValueError:
        return 0


def increment_chat_count(persona: str, n: int = 1) -> int:
    """Add `n` to this persona's monthly chat count and return the new total."""
    total = get_chat_count(persona) + n
    set_setting(_chat_usage_key(persona), str(total))
    return total


def create_lead(name="", phone="", email="", business="", request="",
                source="", notes="", status="New", sms_opt_in=False) -> str:
    """Create a lead record. Returns a short human-readable confirmation."""
    if not is_configured():
        return "The CRM isn't connected yet, so I couldn't save that. (Airtable token not set.)"
    fields = {}
    if name: fields["Name"] = name
    if phone: fields["Phone"] = phone
    if email: fields["Email"] = email
    if business: fields["Business"] = business
    if request: fields["Request"] = request
    if source: fields["Source"] = source
    if notes: fields["Notes"] = notes
    if sms_opt_in: fields["SMS Opt-In"] = True
    fields["Status"] = status or "New"
    try:
        tid = _ensure_table()
        with httpx.Client(timeout=30) as c:
            r = c.post(f"{_API}/v0/{AIRTABLE_BASE_ID}/{tid}",
                       headers=_headers(), json={"fields": fields, "typecast": True})
            r.raise_for_status()
        who = name or phone or "the lead"
        return f"Saved {who} to the CRM ({business or 'unspecified business'}, status New)."
    except Exception as e:
        return f"I couldn't save that to the CRM: {type(e).__name__}. Please try again."


def list_leads(business="", status="", search="", limit=10) -> str:
    """Return a short text list of matching leads, newest first."""
    if not is_configured():
        return "The CRM isn't connected yet. (Airtable token not set.)"
    try:
        tid = _ensure_table()
        # Build an Airtable filter formula from whatever was provided.
        clauses = []
        if business: clauses.append("{Business}='" + business.replace("'", "") + "'")
        if status: clauses.append("{Status}='" + status.replace("'", "") + "'")
        if search:
            s = search.replace("'", "")
            clauses.append("OR(FIND(LOWER('" + s + "'),LOWER({Name})),"
                           "FIND(LOWER('" + s + "'),LOWER({Request})),"
                           "FIND(LOWER('" + s + "'),LOWER({Notes})),"
                           "FIND('" + s + "',{Phone}))")
        params = {"pageSize": str(min(int(limit or 10), 50))}
        if clauses:
            params["filterByFormula"] = "AND(" + ",".join(clauses) + ")" if len(clauses) > 1 else clauses[0]
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{_API}/v0/{AIRTABLE_BASE_ID}/{tid}", headers=_headers(), params=params)
            r.raise_for_status()
        recs = r.json().get("records", [])
        if not recs:
            return "No matching leads found in the CRM."
        lines = []
        for rec in recs[: int(limit or 10)]:
            f = rec.get("fields", {})
            bits = [f.get("Name", "(no name)")]
            if f.get("Phone"): bits.append(f["Phone"] + (" (SMS opt-in)" if f.get("SMS Opt-In") else ""))
            if f.get("Business"): bits.append(f["Business"])
            if f.get("Status"): bits.append(f["Status"])
            if f.get("Request"): bits.append("- " + f["Request"][:80])
            lines.append(" · ".join(bits))
        return "Here are the leads:\n" + "\n".join(lines)
    except Exception as e:
        return f"I couldn't read the CRM: {type(e).__name__}. Please try again."
