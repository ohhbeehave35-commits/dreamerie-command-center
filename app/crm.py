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
import logging
import threading
from typing import Optional
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)

_increment_lock = threading.Lock()

AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN", "")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID", "")
TABLE_NAME = os.environ.get("AIRTABLE_TABLE", "Leads")

_API = "https://api.airtable.com"
_table_id_cache = None  # resolved/created Leads table id
_conv_table_id_cache = None  # resolved/created Conversations table id
CONV_TABLE = "Conversations"
_chat_sessions_table_id_cache = None
CHAT_SESSIONS_TABLE = "ChatSessions"
_build_table_id_cache = None  # resolved/created Build Requests table id
BUILD_TABLE = "Build Requests"
_skills_table_id_cache = None  # resolved/created Skills Log table id
SKILLS_TABLE = "Skills Log"
_settings_table_id_cache = None  # resolved/created Settings table id
SETTINGS_TABLE = "Settings"
_artifacts_table_id_cache = None  # resolved/created Artifacts table id
ARTIFACTS_TABLE = "Artifacts"
_push_table_id_cache = None  # resolved/created Push Subscriptions table id
PUSH_TABLE = "PushSubscriptions"
_strategy_table_id_cache = None  # resolved/created Client Strategies table id
STRATEGY_TABLE = "Client Strategies"
_dev_agent_log_table_id_cache = None
DEV_AGENT_LOG_TABLE = "Dev Agent Log"

# Memory health tracking -- set on every save/load so /api/memory/health
# can report whether persistence is actually working without a round-trip.
_memory_stats = {
    "last_save_ok_ts": None,     # ISO string of most recent successful save
    "last_save_err_ts": None,    # ISO string of most recent failed save
    "last_save_err_msg": "",     # short human-readable error
    "save_success_count": 0,
    "save_fail_count": 0,
    "last_load_ok_ts": None,
    "last_load_err_ts": None,
    "last_load_err_msg": "",
}


def memory_stats() -> dict:
    """Snapshot of memory-persistence health for /api/memory/health."""
    return dict(_memory_stats)


import secrets


def is_configured() -> bool:
    return bool(AIRTABLE_TOKEN and AIRTABLE_BASE_ID)


def _headers() -> dict:
    return {"Authorization": f"Bearer {AIRTABLE_TOKEN}", "Content-Type": "application/json"}


def _formula_literal(value: str) -> str:
    """Escape a value for embedding inside a single-quoted Airtable formula
    string literal, e.g. ``"{Field}='" + _formula_literal(v) + "'"``.

    Airtable formula string literals do NOT honor backslash escaping, so the
    previous ``\\'`` approach was both wrong and unsafe: the ``\\`` became a
    literal backslash and the ``'`` still closed the string. That made the
    injection guard ineffective (an apostrophe still breaks out of the literal)
    AND broke every legitimate value containing an apostrophe -- client names,
    usernames like ``O'Brien`` -- because the lookup formula was malformed.

    Instead, neutralize each apostrophe by closing the single-quoted literal,
    concatenating one literal apostrophe expressed as the double-quoted string
    ``"'"``, then reopening it: every ``'`` becomes ``' & "'" & '``. Since each
    apostrophe is turned into an inert string-concatenation, a hostile value can
    never break out into formula logic (injection-safe) while ordinary
    apostrophes are preserved exactly (``{Field}='O' & "'" & 'Brien'`` matches
    ``O'Brien``). Backslashes are ordinary characters in Airtable formulas, so
    they are left untouched. Works unchanged for every caller because they all
    wrap the result in single quotes -- including the FIND(LOWER('...')) and
    ``.lower()`` sites, where lowercasing the concatenation glue is a no-op.
    """
    return str(value).replace("'", "' & \"'\" & '")


# The Leads table schema Annabelle writes to.
_FIELDS = [
    {"name": "Name", "type": "singleLineText"},
    {"name": "Phone", "type": "singleLineText"},
    {"name": "Email", "type": "email"},
    {"name": "Business", "type": "singleSelect", "options": {"choices": [
        {"name": "The Dreamerie"}, {"name": "Suzy D / TikTok"},
        {"name": "Bear Arms"}, {"name": "Peptides"},
        {"name": "Late Nite Labs"}, {"name": "Other"}]}},
    {"name": "Request", "type": "multilineText"},
    {"name": "Status", "type": "singleSelect", "options": {"choices": [
        {"name": "New"}, {"name": "Contacted"}, {"name": "Quoted"},
        {"name": "Scheduled"}, {"name": "Done"}, {"name": "Lost"}]}},
    {"name": "Source", "type": "singleSelect", "options": {"choices": [
        {"name": "Call"}, {"name": "Text"}, {"name": "Website"},
        {"name": "Referral"}, {"name": "Walk-in"}, {"name": "Other"}]}},
    {"name": "Notes", "type": "multilineText"},
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


def _ensure_field(c: httpx.Client, table_id: str, table_meta: dict, name: str, field_type: str = "singleLineText") -> None:
    """Add a column to an EXISTING table if it isn't there yet. Airtable's
    record-create endpoint 422s on an unrecognized field name -- it does not
    auto-create columns -- so any field added to a table that was already
    created in a prior deploy needs this explicit migration step, run once
    (the table_id cache above means this only fires the first time a given
    process resolves the table, which is cheap and safe to repeat)."""
    existing = {f.get("name") for f in table_meta.get("fields", [])}
    if name in existing:
        return
    r = c.post(f"{_API}/v0/meta/bases/{AIRTABLE_BASE_ID}/tables/{table_id}/fields",
               headers=_headers(), json={"name": name, "type": field_type})
    r.raise_for_status()


def _ensure_chat_sessions_table() -> str:
    """Return the ChatSessions table id, creating it if needed (and adding
    any columns introduced after the table already existed in production)."""
    global _chat_sessions_table_id_cache
    if _chat_sessions_table_id_cache:
        return _chat_sessions_table_id_cache
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{_API}/v0/meta/bases/{AIRTABLE_BASE_ID}/tables", headers=_headers())
        r.raise_for_status()
        for t in r.json().get("tables", []):
            if t.get("name", "").lower() == CHAT_SESSIONS_TABLE.lower():
                _chat_sessions_table_id_cache = t["id"]
                _ensure_field(c, t["id"], t, "Owner", "singleLineText")
                return _chat_sessions_table_id_cache
        fields = [
            {"name": "ChatID", "type": "singleLineText"},
            {"name": "Name", "type": "singleLineText"},
            {"name": "CreatedAt", "type": "singleLineText"},
            {"name": "Owner", "type": "singleLineText"},
        ]
        r = c.post(f"{_API}/v0/meta/bases/{AIRTABLE_BASE_ID}/tables",
                   headers=_headers(), json={"name": CHAT_SESSIONS_TABLE, "fields": fields})
        r.raise_for_status()
        _chat_sessions_table_id_cache = r.json()["id"]
        return _chat_sessions_table_id_cache


def _ensure_conv_table() -> str:
    """Return the Conversations table id, creating it if needed (and adding
    any columns introduced after the table already existed in production)."""
    global _conv_table_id_cache
    if _conv_table_id_cache:
        return _conv_table_id_cache
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{_API}/v0/meta/bases/{AIRTABLE_BASE_ID}/tables", headers=_headers())
        r.raise_for_status()
        for t in r.json().get("tables", []):
            if t.get("name", "").lower() == CONV_TABLE.lower():
                _conv_table_id_cache = t["id"]
                _ensure_field(c, t["id"], t, "ChatID", "singleLineText")
                _ensure_field(c, t["id"], t, "Speaker", "singleLineText")
                return _conv_table_id_cache
        fields = [
            {"name": "Role", "type": "singleLineText"},
            {"name": "Content", "type": "multilineText"},
            {"name": "ChatID", "type": "singleLineText"},
            {"name": "Speaker", "type": "singleLineText"},
        ]
        r = c.post(f"{_API}/v0/meta/bases/{AIRTABLE_BASE_ID}/tables",
                   headers=_headers(), json={"name": CONV_TABLE, "fields": fields})
        r.raise_for_status()
        _conv_table_id_cache = r.json()["id"]
        return _conv_table_id_cache


def create_chat_session(name: str = "Chat", owner: str = "shared") -> str:
    """Create new chat session, return chat_id. `owner` scopes the session to
    one logged-in account (see users.py) so one person's named chats never
    show up in another person's sidebar."""
    if not is_configured():
        return secrets.token_hex(8)
    try:
        chat_id = secrets.token_hex(8)
        tid = _ensure_chat_sessions_table()
        now = datetime.now(timezone.utc).isoformat()
        with httpx.Client(timeout=30) as c:
            c.post(f"{_API}/v0/{AIRTABLE_BASE_ID}/{tid}", headers=_headers(),
                   json={"fields": {"ChatID": chat_id, "Name": name[:100], "CreatedAt": now, "Owner": owner},
                         "typecast": True})
        return chat_id
    except Exception:
        return secrets.token_hex(8)


def get_chat_sessions(owner: str = "shared") -> list:
    """Return chat sessions [{chat_id, name, created_at}] belonging to `owner`,
    newest first. Older rows created before the Owner column existed have no
    value and are treated as "shared" so nothing already-saved disappears."""
    if not is_configured():
        return []
    try:
        tid = _ensure_chat_sessions_table()
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{_API}/v0/{AIRTABLE_BASE_ID}/{tid}", headers=_headers(), params={"pageSize": "100"})
            r.raise_for_status()
        recs = r.json().get("records", [])
        recs.sort(key=lambda x: x.get("createdTime", ""), reverse=True)
        out = []
        for rec in recs:
            fields = rec.get("fields", {})
            if not fields.get("ChatID"):
                continue
            rec_owner = fields.get("Owner") or "shared"
            if rec_owner != owner:
                continue
            out.append({"chat_id": fields.get("ChatID", ""), "name": fields.get("Name", "Chat"),
                        "created_at": fields.get("CreatedAt", "")})
        return out
    except Exception:
        return []


def save_turn(role: str, content: str, chat_id: str = "default", speaker: str = "") -> None:
    """Persist one message to Airtable. `speaker` optionally tags WHO typed/said
    it when several people share one login (e.g. "Jane") -- blank means the
    account's primary owner, so nothing already-saved is affected. LOGS every
    failure -- no more silent losses. Memory-health stats are updated so
    /api/memory/health can report the truth without querying Airtable."""
    if not content:
        return
    if not is_configured():
        _memory_stats["last_save_err_ts"] = datetime.now(timezone.utc).isoformat()
        _memory_stats["last_save_err_msg"] = "AIRTABLE_TOKEN or AIRTABLE_BASE_ID not set"
        _memory_stats["save_fail_count"] += 1
        log.error("MEMORY_SAVE_FAIL chat_id=%s role=%s reason=not_configured", chat_id, role)
        return
    try:
        tid = _ensure_conv_table()
        fields = {"Role": role, "Content": content[:100000], "ChatID": chat_id}
        if speaker:
            fields["Speaker"] = speaker[:80]
        with httpx.Client(timeout=30) as c:
            r = c.post(f"{_API}/v0/{AIRTABLE_BASE_ID}/{tid}", headers=_headers(),
                       json={"fields": fields, "typecast": True})
        if r.status_code >= 400:
            body = (r.text or "")[:400]
            _memory_stats["last_save_err_ts"] = datetime.now(timezone.utc).isoformat()
            _memory_stats["last_save_err_msg"] = f"HTTP {r.status_code}: {body}"
            _memory_stats["save_fail_count"] += 1
            log.error("MEMORY_SAVE_FAIL chat_id=%s role=%s http=%s body=%s", chat_id, role, r.status_code, body)
            return
        _memory_stats["last_save_ok_ts"] = datetime.now(timezone.utc).isoformat()
        _memory_stats["save_success_count"] += 1
        log.info("MEMORY_SAVE_OK chat_id=%s role=%s len=%d", chat_id, role, len(content))
    except Exception as e:
        _memory_stats["last_save_err_ts"] = datetime.now(timezone.utc).isoformat()
        _memory_stats["last_save_err_msg"] = f"{type(e).__name__}: {str(e)[:300]}"
        _memory_stats["save_fail_count"] += 1
        log.exception("MEMORY_SAVE_FAIL chat_id=%s role=%s exception", chat_id, role)


def get_history(limit: int = 40, chat_id: str = "default") -> list:
    """Return last `limit` messages [{role, content}], oldest first, filtered by chat_id.
    Logs failures so we can see WHY memory came back empty."""
    if not is_configured():
        _memory_stats["last_load_err_ts"] = datetime.now(timezone.utc).isoformat()
        _memory_stats["last_load_err_msg"] = "AIRTABLE_TOKEN or AIRTABLE_BASE_ID not set"
        log.error("MEMORY_LOAD_FAIL chat_id=%s reason=not_configured", chat_id)
        return []
    try:
        tid = _ensure_conv_table()
        formula = f"{{ChatID}}='{_formula_literal(chat_id)}'"
        params = {"pageSize": str(min(int(limit or 40), 100)), "filterByFormula": formula}
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{_API}/v0/{AIRTABLE_BASE_ID}/{tid}", headers=_headers(), params=params)
            if r.status_code >= 400:
                body = (r.text or "")[:400]
                _memory_stats["last_load_err_ts"] = datetime.now(timezone.utc).isoformat()
                _memory_stats["last_load_err_msg"] = f"HTTP {r.status_code}: {body}"
                log.error("MEMORY_LOAD_FAIL chat_id=%s http=%s body=%s", chat_id, r.status_code, body)
                return []
        recs = r.json().get("records", [])
        recs.sort(key=lambda x: x.get("createdTime", ""))
        recs = recs[-int(limit or 40):]
        out = [{"role": rec["fields"].get("Role", "user"),
                "content": rec["fields"].get("Content", ""),
                "speaker": rec["fields"].get("Speaker", "")} for rec in recs if rec.get("fields", {}).get("Content")]
        _memory_stats["last_load_ok_ts"] = datetime.now(timezone.utc).isoformat()
        log.info("MEMORY_LOAD_OK chat_id=%s returned=%d", chat_id, len(out))
        return out
    except Exception as e:
        _memory_stats["last_load_err_ts"] = datetime.now(timezone.utc).isoformat()
        _memory_stats["last_load_err_msg"] = f"{type(e).__name__}: {str(e)[:300]}"
        log.exception("MEMORY_LOAD_FAIL chat_id=%s exception", chat_id)
        return []


def get_all_history(limit: int = 9999) -> list:
    """Return up to `limit` messages [{chat_id, role, content, speaker}] across
    EVERY chat_id (all accounts), oldest first. Used for full-backup export --
    get_history() only returns one chat_id at a time, which would silently
    export nothing for namespaced per-account chat_ids like 'user:Boss:default'."""
    if not is_configured():
        return []
    try:
        tid = _ensure_conv_table()
        recs = []
        params = {"pageSize": "100"}
        with httpx.Client(timeout=30) as c:
            while len(recs) < limit:
                r = c.get(f"{_API}/v0/{AIRTABLE_BASE_ID}/{tid}", headers=_headers(), params=params)
                if r.status_code >= 400:
                    log.error("MEMORY_EXPORT_FAIL http=%s body=%s", r.status_code, (r.text or "")[:400])
                    break
                body = r.json()
                recs.extend(body.get("records", []))
                offset = body.get("offset")
                if not offset:
                    break
                params["offset"] = offset
        recs.sort(key=lambda x: x.get("createdTime", ""))
        recs = recs[:limit]
        return [{"chat_id": rec["fields"].get("ChatID", ""),
                 "role": rec["fields"].get("Role", "user"),
                 "content": rec["fields"].get("Content", ""),
                 "speaker": rec["fields"].get("Speaker", "")} for rec in recs if rec.get("fields", {}).get("Content")]
    except Exception:
        log.exception("MEMORY_EXPORT_FAIL exception")
        return []


def _ensure_artifacts_table() -> str:
    """Return the Artifacts table id, creating it if needed."""
    global _artifacts_table_id_cache
    if _artifacts_table_id_cache:
        return _artifacts_table_id_cache
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{_API}/v0/meta/bases/{AIRTABLE_BASE_ID}/tables", headers=_headers())
        r.raise_for_status()
        for t in r.json().get("tables", []):
            if t.get("name", "").lower() == ARTIFACTS_TABLE.lower():
                _artifacts_table_id_cache = t["id"]
                return _artifacts_table_id_cache
        fields = [
            {"name": "Slug", "type": "singleLineText"},
            {"name": "Title", "type": "singleLineText"},
            {"name": "Content", "type": "multilineText"},
            {"name": "CreatedAt", "type": "singleLineText"},
        ]
        r = c.post(f"{_API}/v0/meta/bases/{AIRTABLE_BASE_ID}/tables",
                   headers=_headers(), json={"name": ARTIFACTS_TABLE, "fields": fields})
        r.raise_for_status()
        _artifacts_table_id_cache = r.json()["id"]
        return _artifacts_table_id_cache


def create_artifact(title: str, content: str) -> str:
    """Persist a generated document (proposal, audit, long-form content) and
    return a slug it can be viewed at (/artifact/{slug}). Returns a slug
    immediately (fire-and-forget save to Airtable in background)."""
    if not is_configured() or not content:
        return ""
    slug = secrets.token_hex(8)
    # Fire-and-forget: save to Airtable in background thread so the
    # response isn't blocked by network latency (30s timeout on slow connections).
    def _save_artifact():
        try:
            tid = _ensure_artifacts_table()
            now = datetime.now(timezone.utc).isoformat()
            with httpx.Client(timeout=30) as c:
                c.post(f"{_API}/v0/{AIRTABLE_BASE_ID}/{tid}", headers=_headers(),
                       json={"fields": {"Slug": slug, "Title": (title or "Document")[:200],
                                         "Content": content[:100000], "CreatedAt": now}, "typecast": True})
        except Exception:
            pass  # Silent: user already got the slug and can view the artifact
    import threading
    threading.Thread(target=_save_artifact, daemon=True).start()
    return slug


def get_artifact(slug: str) -> dict:
    """Return {title, content} for a stored artifact, or {} if not found."""
    if not is_configured() or not slug:
        return {}
    try:
        tid = _ensure_artifacts_table()
        formula = "{Slug}='" + slug.replace("'", "") + "'"
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{_API}/v0/{AIRTABLE_BASE_ID}/{tid}", headers=_headers(),
                      params={"filterByFormula": formula, "pageSize": "1"})
            r.raise_for_status()
        recs = r.json().get("records", [])
        if not recs:
            return {}
        f = recs[0]["fields"]
        return {"title": f.get("Title", "Document"), "content": f.get("Content", "")}
    except Exception:
        return {}


def _ensure_strategy_table() -> str:
    """Return the Client Strategies table id, creating it if needed."""
    global _strategy_table_id_cache
    if _strategy_table_id_cache:
        return _strategy_table_id_cache
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{_API}/v0/meta/bases/{AIRTABLE_BASE_ID}/tables", headers=_headers())
        r.raise_for_status()
        for t in r.json().get("tables", []):
            if t.get("name", "").lower() == STRATEGY_TABLE.lower():
                _strategy_table_id_cache = t["id"]
                _ensure_field(c, t["id"], t, "Kind", "singleLineText")
                _ensure_field(c, t["id"], t, "Priority", "singleLineText")
                _ensure_field(c, t["id"], t, "Content", "multilineText")
                _ensure_field(c, t["id"], t, "UpdatedAt", "singleLineText")
                return _strategy_table_id_cache
        fields = [
            {"name": "Client", "type": "singleLineText"},   # primary: company name
            {"name": "Kind", "type": "singleLineText"},     # e.g. "sales_strategy", "intel"
            {"name": "Priority", "type": "singleLineText"}, # high / normal / low
            {"name": "Content", "type": "multilineText"},
            {"name": "UpdatedAt", "type": "singleLineText"},
        ]
        r = c.post(f"{_API}/v0/meta/bases/{AIRTABLE_BASE_ID}/tables",
                   headers=_headers(), json={"name": STRATEGY_TABLE, "fields": fields})
        r.raise_for_status()
        _strategy_table_id_cache = r.json()["id"]
        return _strategy_table_id_cache


def save_strategy(client: str, content: str, kind: str = "sales_strategy",
                  priority: str = "normal") -> dict:
    """Store (or replace) a client strategy Annabelle can pull at chat time.

    Upserts on (Client, Kind) so re-pushing a revised strategy overwrites the
    old one rather than leaving two contradictory versions for her to find."""
    if not client or not content:
        return {"ok": False, "error": "client and content are both required"}
    if not is_configured():
        return {"ok": False, "error": "Airtable isn't connected"}
    try:
        tid = _ensure_strategy_table()
        now = datetime.now(timezone.utc).isoformat()
        payload = {"Client": client[:200], "Kind": kind[:60],
                   "Priority": priority[:20], "Content": content[:100000],
                   "UpdatedAt": now}
        safe_client = _formula_literal(client)
        safe_kind = _formula_literal(kind)
        formula = "AND({Client}='" + safe_client + "',{Kind}='" + safe_kind + "')"
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{_API}/v0/{AIRTABLE_BASE_ID}/{tid}", headers=_headers(),
                      params={"filterByFormula": formula, "pageSize": "1"})
            existing = r.json().get("records", []) if r.status_code < 400 else []
            if existing:
                rid = existing[0]["id"]
                r = c.patch(f"{_API}/v0/{AIRTABLE_BASE_ID}/{tid}/{rid}", headers=_headers(),
                            json={"fields": payload, "typecast": True})
                action = "updated"
            else:
                r = c.post(f"{_API}/v0/{AIRTABLE_BASE_ID}/{tid}", headers=_headers(),
                           json={"fields": payload, "typecast": True})
                action = "created"
        if r.status_code >= 400:
            body = (r.text or "")[:400]
            log.error("STRATEGY_SAVE_FAIL client=%s kind=%s http=%s body=%s",
                      client, kind, r.status_code, body)
            return {"ok": False, "error": f"Airtable HTTP {r.status_code}: {body}"}
        rid = r.json().get("id", "")
        log.info("STRATEGY_SAVE_OK client=%s kind=%s action=%s id=%s", client, kind, action, rid)
        return {"ok": True, "action": action, "id": rid, "client": client, "kind": kind}
    except Exception as e:
        log.exception("STRATEGY_SAVE_FAIL client=%s kind=%s exception", client, kind)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def get_strategy(client: str, kind: str = "") -> list:
    """Return stored strategy records for a client (all kinds if kind is empty)."""
    if not is_configured() or not client:
        return []
    try:
        tid = _ensure_strategy_table()
        safe_client = _formula_literal(client)
        formula = "LOWER({Client})='" + safe_client.lower() + "'"
        if kind:
            formula = "AND(" + formula + ",{Kind}='" + _formula_literal(kind) + "')"
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{_API}/v0/{AIRTABLE_BASE_ID}/{tid}", headers=_headers(),
                      params={"filterByFormula": formula, "pageSize": "10"})
            r.raise_for_status()
        return [{"id": rec["id"],
                 "client": rec["fields"].get("Client", ""),
                 "kind": rec["fields"].get("Kind", ""),
                 "priority": rec["fields"].get("Priority", "normal"),
                 "content": rec["fields"].get("Content", ""),
                 "updated_at": rec["fields"].get("UpdatedAt", "")}
                for rec in r.json().get("records", [])]
    except Exception:
        log.exception("STRATEGY_READ_FAIL client=%s kind=%s exception", client, kind)
        return []


def list_strategies() -> list:
    """Return every stored strategy, high priority first. Content is omitted --
    this is the index, not the payload."""
    if not is_configured():
        return []
    try:
        tid = _ensure_strategy_table()
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{_API}/v0/{AIRTABLE_BASE_ID}/{tid}", headers=_headers(),
                      params={"pageSize": "100"})
            r.raise_for_status()
        recs = r.json().get("records", [])
        recs.sort(key=lambda x: (
            {"high": 0, "normal": 1, "low": 2}.get(
                str(x.get("fields", {}).get("Priority", "normal")).lower(), 3),
            x.get("fields", {}).get("Client", ""),
        ))
        return [{"id": rec["id"],
                 "client": rec["fields"].get("Client", ""),
                 "kind": rec["fields"].get("Kind", ""),
                 "priority": rec["fields"].get("Priority", "normal"),
                 "chars": len(rec["fields"].get("Content", "")),
                 "updated_at": rec["fields"].get("UpdatedAt", "")}
                for rec in recs if rec["fields"].get("Client")]
    except Exception:
        log.exception("STRATEGY_LIST_FAIL exception")
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
                _ensure_field(c, t["id"], t, "Request", "singleLineText")
                _ensure_field(c, t["id"], t, "Details", "multilineText")
                _ensure_field(c, t["id"], t, "Status", "singleSelect")
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


def _ensure_skills_table() -> str:
    """Return the Skills Log table id, creating it if needed. Durable notes
    Annabelle writes about lessons/patterns/gotchas worth remembering for
    future dev work -- a Claude Code session reads Status=New rows and folds
    them into docs/SKILLS.md, then marks them Synced so they aren't reused."""
    global _skills_table_id_cache
    if _skills_table_id_cache:
        return _skills_table_id_cache
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{_API}/v0/meta/bases/{AIRTABLE_BASE_ID}/tables", headers=_headers())
        r.raise_for_status()
        for t in r.json().get("tables", []):
            if t.get("name", "").lower() == SKILLS_TABLE.lower():
                _skills_table_id_cache = t["id"]
                _ensure_field(c, t["id"], t, "Title", "singleLineText")
                _ensure_field(c, t["id"], t, "Note", "multilineText")
                _ensure_field(c, t["id"], t, "Category", "singleLineText")
                _ensure_field(c, t["id"], t, "Status", "singleSelect")
                return _skills_table_id_cache
        fields = [
            {"name": "Title", "type": "singleLineText"},     # primary: plain text
            {"name": "Note", "type": "multilineText"},
            {"name": "Category", "type": "singleLineText"},
            {"name": "Status", "type": "singleSelect", "options": {"choices": [
                {"name": "New"}, {"name": "Synced"}]}},
        ]
        r = c.post(f"{_API}/v0/meta/bases/{AIRTABLE_BASE_ID}/tables",
                   headers=_headers(), json={"name": SKILLS_TABLE, "fields": fields})
        r.raise_for_status()
        _skills_table_id_cache = r.json()["id"]
        return _skills_table_id_cache


def log_skill_note(title: str = "", note: str = "", category: str = "") -> str:
    """Save a durable lesson/pattern/gotcha for future dev work. Writes to
    Airtable (primary, queryable) and best-effort appends a backup copy to
    Dropbox in case Airtable is ever unavailable. Never raises -- always
    returns something Annabelle can say."""
    title, note, category = (title or "").strip(), (note or "").strip(), (category or "").strip()
    if not title or not note:
        return "I need both a short title and the actual note to save."

    airtable_ok = False
    airtable_err = ""
    if is_configured():
        try:
            tid = _ensure_skills_table()
            with httpx.Client(timeout=30) as c:
                r = c.post(f"{_API}/v0/{AIRTABLE_BASE_ID}/{tid}", headers=_headers(),
                           json={"fields": {
                               "Title": title[:200], "Note": note[:5000],
                               "Category": category[:100], "Status": "New",
                           }, "typecast": True})
            airtable_ok = r.status_code < 400
            if not airtable_ok:
                airtable_err = f"HTTP {r.status_code}"
        except Exception as e:
            airtable_err = f"{type(e).__name__}: {e}"
    else:
        airtable_err = "Airtable not connected"

    dropbox_ok = False
    dropbox_err = ""
    try:
        from . import files_dropbox
        if files_dropbox.is_configured():
            entry = (f"\n---\n**{title}**"
                     + (f" ({category})" if category else "")
                     + f" — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n{note}\n")
            dropbox_ok, dropbox_err = files_dropbox.append_text_file(
                "/Assistant Notes/skills-log.md", entry)
        else:
            dropbox_err = "Dropbox not connected"
    except Exception as e:
        dropbox_err = f"{type(e).__name__}: {e}"

    if airtable_ok:
        return f"Noted: {title}." + ("" if dropbox_ok else f" (Dropbox backup skipped: {dropbox_err})")
    if dropbox_ok:
        return f"Noted: {title} (saved to Dropbox only -- Airtable: {airtable_err})"
    return f"Couldn't save that anywhere right now (Airtable: {airtable_err}; Dropbox: {dropbox_err}) -- but here it is so you don't lose it: {title} — {note}"


def _forward_build_request_email(request: str, details: str, ticket_id: str = "") -> None:
    """Fire-and-forget: email the ticket to the owner so nothing lives only in
    Airtable. Safe to call even if Gmail isn't set up — silently no-ops."""
    try:
        from . import emailer
        to = emailer.get_gmail_address()
        if not to or not emailer.is_configured():
            return
        subject = f"[Assistant Ticket] {request[:80]}"
        body = (
            "Annabelle just logged a new build request.\n\n"
            f"Request: {request}\n"
            f"Details: {details or '(none)'}\n"
            f"Ticket id: {ticket_id or '(none)'}\n\n"
            "Open the Command Center → Pending Requests panel to work it, or "
            "click 'Copy for Claude' on the row to get a ready-to-paste "
            "prompt for a Claude Code session.\n"
        )
        threading.Thread(
            target=lambda: emailer.send_email(to, subject, body),
            daemon=True,
        ).start()
    except Exception as e:
        log.warning("BUILD_REQUEST_FORWARD_FAIL exception=%s", e)


def _push_build_request_alert(request: str, ticket_id: str = "") -> None:
    """Fire-and-forget: phone-lock-screen push, independent of whether
    Annabelle's reply happened to include an [[ALERT: ...]] marker. Every
    ticket gets this — a structural guarantee, not a prompt hope. Safe to
    call even if push isn't set up — silently no-ops."""
    try:
        from . import push
        if not push.is_configured():
            return
        threading.Thread(
            target=push.send_to_owner,
            args=("New Build Request", request[:180], "/static/index.html?panel=pending"),
            daemon=True,
        ).start()
    except Exception as e:
        log.warning("BUILD_REQUEST_PUSH_FAIL exception=%s", e)


def create_build_request(request="", details="") -> str:
    """Queue a capability/feature Annabelle is missing, for the dev team to build."""
    if not request:
        return "I need a short description of what to build."
    if not is_configured():
        return f"Noted this to build: {request}. (The build queue isn't connected yet.)"
    try:
        tid = _ensure_build_table()
        with httpx.Client(timeout=30) as c:
            r = c.post(f"{_API}/v0/{AIRTABLE_BASE_ID}/{tid}", headers=_headers(),
                       json={"fields": {"Request": request[:500], "Details": details, "Status": "New"}, "typecast": True})
        if r.status_code >= 400:
            body = (r.text or "")[:400]
            log.error("BUILD_REQUEST_SAVE_FAIL request=%s http=%s body=%s", request, r.status_code, body)
            return f"I couldn't log that build request (Airtable HTTP {r.status_code}), but I've noted it: {request}"
        try:
            ticket_id = r.json().get("id", "")
        except Exception:
            ticket_id = ""
        _forward_build_request_email(request, details, ticket_id)
        _push_build_request_alert(request, ticket_id)
        log.info("BUILD_REQUEST_SAVE_OK request=%s ticket=%s", request, ticket_id)
        return f"Logged a build request for the dev team: {request}"
    except Exception as e:
        log.exception("BUILD_REQUEST_SAVE_FAIL request=%s exception", request)
        return f"I couldn't log that build request ({type(e).__name__}), but I've noted it: {request}"


def get_pending_requests() -> list:
    """Return all build requests, New/Building first, then by creation date."""
    if not is_configured():
        return []
    try:
        tid = _ensure_build_table()
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{_API}/v0/{AIRTABLE_BASE_ID}/{tid}", headers=_headers(), params={"pageSize": "100"})
            r.raise_for_status()
        recs = r.json().get("records", [])
        recs.sort(key=lambda x: (
            {"New": 0, "Building": 1, "Done": 2}.get(x.get("fields", {}).get("Status", "New"), 3),
            x.get("createdTime", "")
        ))
        return [{"id": rec["id"], "request": rec["fields"].get("Request", ""),
                 "details": rec["fields"].get("Details", ""),
                 "status": rec["fields"].get("Status", "New")} for rec in recs if rec["fields"].get("Request")]
    except Exception:
        log.exception("BUILD_REQUEST_READ_FAIL exception")
        return []


def update_request_status(record_id: str, status: str) -> bool:
    """Update a build request's status. Returns True on success."""
    if not is_configured() or status not in ("New", "Building", "Done"):
        return False
    try:
        tid = _ensure_build_table()
        with httpx.Client(timeout=30) as c:
            r = c.patch(f"{_API}/v0/{AIRTABLE_BASE_ID}/{tid}/{record_id}", headers=_headers(),
                        json={"fields": {"Status": status}, "typecast": True})
        if r.status_code >= 400:
            log.error("BUILD_REQUEST_STATUS_FAIL id=%s status=%s http=%s body=%s",
                      record_id, status, r.status_code, (r.text or "")[:400])
            return False
        return True
    except Exception:
        log.exception("BUILD_REQUEST_STATUS_FAIL id=%s status=%s exception", record_id, status)
        return False


# ---- Verification Log -------------------------------------------------------
# Every answer Annabelle gives that touches pricing, scheduling, or a fact she
# had to search for gets one row here: what was asked, which tier resolved it
# (Ground Truth / Verified / Escalated), and what the source was. This is the
# audit trail behind the "how does Annabelle check her facts" verification
# framework -- it's what lets Vinny (or a customer, on request) see exactly
# where an answer came from instead of taking it on faith.
_verification_table_id_cache = None
VERIFICATION_TABLE = "Verification Log"


def _ensure_verification_table() -> str:
    """Return the Verification Log table id, creating it if needed."""
    global _verification_table_id_cache
    if _verification_table_id_cache:
        return _verification_table_id_cache
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{_API}/v0/meta/bases/{AIRTABLE_BASE_ID}/tables", headers=_headers())
        r.raise_for_status()
        for t in r.json().get("tables", []):
            if t.get("name", "").lower() == VERIFICATION_TABLE.lower():
                _verification_table_id_cache = t["id"]
                return _verification_table_id_cache
        fields = [
            {"name": "Question", "type": "singleLineText"},   # primary: plain text
            {"name": "Tier", "type": "singleSelect", "options": {"choices": [
                {"name": "Ground Truth"}, {"name": "Verified"}, {"name": "Escalated"}]}},
            {"name": "Source", "type": "singleLineText"},
            {"name": "Detail", "type": "multilineText"},
        ]
        r = c.post(f"{_API}/v0/meta/bases/{AIRTABLE_BASE_ID}/tables",
                   headers=_headers(), json={"name": VERIFICATION_TABLE, "fields": fields})
        r.raise_for_status()
        _verification_table_id_cache = r.json()["id"]
        return _verification_table_id_cache


def log_verification(question: str, tier: str, source: str = "", detail: str = "") -> None:
    """Fire-and-forget: record one verification-tier decision. Never raises
    and never blocks the response -- this is an audit trail, not something a
    reply should ever wait on or fail because of."""
    if not is_configured() or tier not in ("Ground Truth", "Verified", "Escalated"):
        return

    def _persist():
        try:
            tid = _ensure_verification_table()
            with httpx.Client(timeout=30) as c:
                c.post(f"{_API}/v0/{AIRTABLE_BASE_ID}/{tid}", headers=_headers(), json={
                    "fields": {
                        "Question": (question or "")[:500],
                        "Tier": tier,
                        "Source": (source or "")[:200],
                        "Detail": (detail or "")[:2000],
                    },
                    "typecast": True,
                })
        except Exception:
            log.warning("VERIFICATION_LOG_FAIL question=%s tier=%s", question, tier, exc_info=True)

    threading.Thread(target=_persist, daemon=True).start()


def _ensure_settings_table() -> str:
    """Return the Settings table id, creating it if needed. Key/Value store
    used for small persisted state like the search-usage counter."""
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
        # Primary (first) field must be plain text, per the Airtable gotcha.
        fields = [
            {"name": "Key", "type": "singleLineText"},
            {"name": "Value", "type": "singleLineText"},
        ]
        r = c.post(f"{_API}/v0/meta/bases/{AIRTABLE_BASE_ID}/tables",
                   headers=_headers(), json={"name": SETTINGS_TABLE, "fields": fields})
        r.raise_for_status()
        _settings_table_id_cache = r.json()["id"]
        return _settings_table_id_cache


# Short-TTL snapshot cache for the Settings table. Measured on production:
# per-key reads stacked ~2.5s of Airtable round-trips BEFORE the model even
# started (caps, usage counts, voice, gmail, webhook are all Settings keys).
# One list call now fetches every key at once and is reused for TTL seconds;
# writes update the cache in place so this instance always sees its own
# changes immediately. Admin edits from the Settings panel still take effect
# within TTL seconds -- the zero-redeploy promise holds.
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
    """Read one Key/Value setting (via the snapshot cache). Returns `default`
    if unset or unconfigured."""
    if not is_configured():
        return default
    try:
        return _settings_snapshot().get(key, default)
    except Exception:
        return default


def set_setting(key: str, value: str, sync: bool = False) -> bool:
    """Write one Key/Value setting (creates or updates). Updates the snapshot
    cache immediately; persists to Airtable in background by default so chat
    latency isn't blocked by network calls.

    sync=True runs the Airtable write inline and returns whether it actually
    landed. Admin "Save" buttons must use this: the background path shows the
    new value from cache for TTL seconds even when the write failed, so the UI
    says "Saved.", the user walks away, and 30s later the old value is back.
    That is exactly how Susan's Zapier webhook URLs kept "not saving" with no
    error anywhere.

    Return value: True when the write landed (or, on the async path, was
    handed to the background writer); False when it is KNOWN to have failed.
    Failures are also recorded on the /support page either way -- a write that
    fails only in a daemon thread used to be a write that failed in private.
    """
    if not is_configured():
        return False
    # Update cache immediately so this instance reads its own write without TTL wait.
    global _settings_cache, _settings_cache_at
    if _settings_cache_at:
        _settings_cache[key] = str(value)

    def _persist_setting() -> bool:
        try:
            tid = _ensure_settings_table()
            with httpx.Client(timeout=30) as c:
                r = c.get(f"{_API}/v0/{AIRTABLE_BASE_ID}/{tid}", headers=_headers(),
                          params={"filterByFormula": "{Key}='" + _formula_literal(key) + "'", "pageSize": "1"})
                r.raise_for_status()
                recs = r.json().get("records", [])
                if recs:
                    w = c.patch(f"{_API}/v0/{AIRTABLE_BASE_ID}/{tid}/{recs[0]['id']}",
                                headers=_headers(), json={"fields": {"Value": str(value)}, "typecast": True})
                else:
                    w = c.post(f"{_API}/v0/{AIRTABLE_BASE_ID}/{tid}", headers=_headers(),
                               json={"fields": {"Key": key, "Value": str(value)}, "typecast": True})
                # The old code never looked at the write's status, so a 403
                # (PAT missing write scope) or 422 was a *silent success*.
                w.raise_for_status()
            return True
        except Exception as e:
            log.error("SETTING_WRITE_FAIL key=%s: %s", key, e)
            try:
                from . import support
                support.record_note("setting_write_failed",
                                    f"'{key}' did not persist to Airtable: "
                                    f"{type(e).__name__}: {e}")
            except Exception:
                pass
            # Drop the optimistic cache entry so reads stop claiming a value
            # Airtable doesn't hold; the next snapshot refresh restores truth.
            if _settings_cache_at:
                _settings_cache.pop(key, None)
            return False

    if sync:
        return _persist_setting()
    import threading
    threading.Thread(target=_persist_setting, daemon=True).start()
    return True


def get_user_setting(username: str, key: str, default: str = "") -> str:
    """Read a per-user setting, stored under 'user:{username}:{key}'.
    Falls back to `default` if unset or Airtable not configured."""
    if not is_configured() or not username:
        return default
    return get_setting(f"user:{username}:{key}", default)


def set_user_setting(username: str, key: str, value: str) -> None:
    """Write a per-user setting under 'user:{username}:{key}'.
    Uses the same Settings table and cache-then-persist pattern as set_setting."""
    if not is_configured() or not username:
        return
    set_setting(f"user:{username}:{key}", value)


def _ensure_push_table() -> str:
    """Return the PushSubscriptions table id, creating it if needed. Each
    record is one browser/device's Web Push subscription (endpoint URL +
    the two keys the push service needs to encrypt payloads)."""
    global _push_table_id_cache
    if _push_table_id_cache:
        return _push_table_id_cache
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{_API}/v0/meta/bases/{AIRTABLE_BASE_ID}/tables", headers=_headers())
        r.raise_for_status()
        for t in r.json().get("tables", []):
            if t.get("name", "").lower() == PUSH_TABLE.lower():
                _push_table_id_cache = t["id"]
                return _push_table_id_cache
        fields = [
            {"name": "Endpoint", "type": "singleLineText"},  # primary
            {"name": "P256dh", "type": "singleLineText"},
            {"name": "Auth", "type": "singleLineText"},
            {"name": "UserAgent", "type": "singleLineText"},
            {"name": "CreatedAt", "type": "singleLineText"},
        ]
        r = c.post(f"{_API}/v0/meta/bases/{AIRTABLE_BASE_ID}/tables",
                   headers=_headers(), json={"name": PUSH_TABLE, "fields": fields})
        r.raise_for_status()
        _push_table_id_cache = r.json()["id"]
        return _push_table_id_cache


def add_push_subscription(endpoint: str, p256dh: str, auth: str, user_agent: str = "") -> bool:
    """Store (or refresh) a Web Push subscription, keyed by its unique endpoint
    URL. Safe to call repeatedly -- upserts rather than duplicating."""
    if not is_configured() or not endpoint:
        return False
    try:
        tid = _ensure_push_table()
        formula = "{Endpoint}='" + endpoint.replace("'", "") + "'"
        fields = {
            "Endpoint": endpoint, "P256dh": p256dh, "Auth": auth,
            "UserAgent": user_agent[:200], "CreatedAt": datetime.now(timezone.utc).isoformat(),
        }
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{_API}/v0/{AIRTABLE_BASE_ID}/{tid}", headers=_headers(),
                      params={"filterByFormula": formula, "pageSize": "1"})
            r.raise_for_status()
            recs = r.json().get("records", [])
            if recs:
                c.patch(f"{_API}/v0/{AIRTABLE_BASE_ID}/{tid}/{recs[0]['id']}",
                        headers=_headers(), json={"fields": fields, "typecast": True})
            else:
                c.post(f"{_API}/v0/{AIRTABLE_BASE_ID}/{tid}", headers=_headers(),
                       json={"fields": fields, "typecast": True})
        return True
    except Exception as e:
        log.warning("add_push_subscription failed: %s", e)
        return False


def _ensure_dev_agent_log_table() -> str:
    """Return the Dev Agent Log table id, creating if needed."""
    global _dev_agent_log_table_id_cache
    if _dev_agent_log_table_id_cache:
        return _dev_agent_log_table_id_cache
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{_API}/v0/meta/bases/{AIRTABLE_BASE_ID}/tables", headers=_headers())
        r.raise_for_status()
        for t in r.json().get("tables", []):
            if t.get("name", "").lower() == DEV_AGENT_LOG_TABLE.lower():
                _dev_agent_log_table_id_cache = t["id"]
                return _dev_agent_log_table_id_cache
        fields = [
            {"name": "TicketID", "type": "singleLineText"},  # primary
            {"name": "Action", "type": "singleLineText"},
            {"name": "ApprovalLevel", "type": "singleLineText"},
            {"name": "Result", "type": "multilineText"},
            {"name": "ChangedFiles", "type": "multilineText"},
            {"name": "Error", "type": "multilineText"},
            {"name": "Timestamp", "type": "singleLineText"},
        ]
        r = c.post(f"{_API}/v0/meta/bases/{AIRTABLE_BASE_ID}/tables",
                   headers=_headers(), json={"name": DEV_AGENT_LOG_TABLE, "fields": fields})
        r.raise_for_status()
        _dev_agent_log_table_id_cache = r.json()["id"]
        return _dev_agent_log_table_id_cache


def save_dev_agent_log(ticket_id: str, action: str, approval_level: str, result: str = "", changed_files: list = None, error: str = "") -> bool:
    """Log a dev agent execution to Airtable."""
    if not is_configured():
        return False
    try:
        tid = _ensure_dev_agent_log_table()
        fields = {
            "TicketID": ticket_id,
            "Action": action[:200],
            "ApprovalLevel": approval_level,
            "Result": result[:10000],
            "ChangedFiles": "\n".join(changed_files or [])[:5000],
            "Error": error[:5000],
            "Timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with httpx.Client(timeout=30) as c:
            c.post(f"{_API}/v0/{AIRTABLE_BASE_ID}/{tid}",
                   headers=_headers(), json={"fields": fields, "typecast": True})
        return True
    except Exception as e:
        log.warning("save_dev_agent_log failed: %s", e)
        return False


def list_push_subscriptions() -> list:
    """Return every stored Web Push subscription as
    [{endpoint, p256dh, auth}, ...]."""
    if not is_configured():
        return []
    try:
        tid = _ensure_push_table()
        out = []
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
                    if f.get("Endpoint"):
                        out.append({"id": rec["id"], "endpoint": f["Endpoint"],
                                    "p256dh": f.get("P256dh", ""), "auth": f.get("Auth", "")})
                offset = j.get("offset")
                if not offset:
                    break
        return out
    except Exception:
        return []


def remove_push_subscription(endpoint: str) -> None:
    """Delete a subscription, e.g. after the push service reports it's gone
    (410/404) or the user explicitly unsubscribes."""
    if not is_configured() or not endpoint:
        return
    try:
        tid = _ensure_push_table()
        formula = "{Endpoint}='" + endpoint.replace("'", "") + "'"
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{_API}/v0/{AIRTABLE_BASE_ID}/{tid}", headers=_headers(),
                      params={"filterByFormula": formula, "pageSize": "1"})
            r.raise_for_status()
            recs = r.json().get("records", [])
            if recs:
                c.delete(f"{_API}/v0/{AIRTABLE_BASE_ID}/{tid}/{recs[0]['id']}", headers=_headers())
    except Exception:
        pass


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
    with _increment_lock:
        total = get_search_count() + n
        set_setting(_search_usage_key(), str(total))
        return total


def _media_usage_key(kind: str) -> str:
    """Monthly bucket key per media kind ('image'/'video'), e.g.
    'media_count_image_2026-07', so each kind's cap resets independently."""
    return f"media_count_{kind}_" + datetime.now(timezone.utc).strftime("%Y-%m")


def get_media_count(kind: str) -> int:
    """How many image/video generations ('image' or 'video') have been used
    this calendar month."""
    try:
        return int(get_setting(_media_usage_key(kind), "0") or 0)
    except ValueError:
        return 0


def increment_media_count(kind: str, n: int = 1) -> int:
    """Add `n` to this month's image/video generation count and return the
    new total."""
    with _increment_lock:
        total = get_media_count(kind) + n
        set_setting(_media_usage_key(kind), str(total))
        return total


def _chat_usage_key(persona: str) -> str:
    """Monthly bucket key per persona, e.g. 'chat_count_public_2026-07'."""
    return f"chat_count_{persona}_" + datetime.now(timezone.utc).strftime("%Y-%m")


def _parse_cap(raw, default: int) -> int:
    """Parse a cap value, returning the default if non-positive or non-numeric."""
    try:
        val = int(raw)
        if val > 0:
            return val
        log.warning("Cap value %r is non-positive — using default %d", raw, default)
        return default
    except (TypeError, ValueError):
        log.warning("Invalid cap value %r — using default %d", raw, default)
        return default


def get_chat_count(persona: str) -> int:
    """How many chat turns this persona has used this calendar month."""
    try:
        return int(get_setting(_chat_usage_key(persona), "0") or 0)
    except ValueError:
        return 0


def increment_chat_count(persona: str, n: int = 1) -> int:
    """Add `n` to this persona's monthly chat count and return the new total.
    Serialized with a lock to prevent read-modify-write races under concurrent load."""
    with _increment_lock:
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
    if sms_opt_in: fields["SMS Opt-In"] = True
    if request: fields["Request"] = request
    if source: fields["Source"] = source
    if notes: fields["Notes"] = notes
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


def get_leads_raw(limit: int = 20) -> list:
    """Return raw lead records as dicts for the /api/leads JSON endpoint."""
    if not is_configured():
        return []
    try:
        tid = _ensure_table()
        params = {"pageSize": str(min(limit, 50)), "sort[0][field]": "Created", "sort[0][direction]": "desc"}
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{_API}/v0/{AIRTABLE_BASE_ID}/{tid}", headers=_headers(), params=params)
            r.raise_for_status()
        out = []
        for rec in r.json().get("records", []):
            f = rec.get("fields", {})
            out.append({
                "id": rec.get("id"),
                "name": f.get("Name", ""),
                "email": f.get("Email", ""),
                "phone": f.get("Phone", ""),
                "service": f.get("Request", "") or f.get("Service", ""),
                "status": f.get("Status", ""),
                "created_at": f.get("Created", ""),
            })
        return out
    except Exception:
        return []


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
            if f.get("Phone"): bits.append(f["Phone"])
            if f.get("Business"): bits.append(f["Business"])
            if f.get("Status"): bits.append(f["Status"])
            if f.get("Request"): bits.append("- " + f["Request"][:80])
            lines.append(" · ".join(bits))
        return "Here are the leads:\n" + "\n".join(lines)
    except Exception as e:
        return f"I couldn't read the CRM: {type(e).__name__}. Please try again."


# ---------------------------------------------------------------------------
# Reset for a new customer (super-admin only; called after Q.C. sign-off).
#
# Wipes CUSTOMER-DATA tables + client identity so the next client starts clean.
# Deliberately does NOT touch Settings secrets or Users (wiping either could
# break the deployment or lock the operator out). Best-effort per table -- an
# absent table is skipped, an errored one is logged, the rest still run.
# ---------------------------------------------------------------------------

_CUSTOMER_DATA_TABLES = [
    TABLE_NAME,            # Leads
    CONV_TABLE,            # Conversations
    CHAT_SESSIONS_TABLE,   # ChatSessions
    BUILD_TABLE,           # Build Requests
    SKILLS_TABLE,          # Skills Log
    ARTIFACTS_TABLE,       # Artifacts
    PUSH_TABLE,            # PushSubscriptions
    STRATEGY_TABLE,        # Client Strategies
    DEV_AGENT_LOG_TABLE,   # Dev Agent Log
    VERIFICATION_TABLE,    # Verification Log
]

_IDENTITY_SETTING_KEYS = [
    "assistant_name",
    "brand_accent",
    "brand_logo_url",
    "owner_password_changed",
]


def _table_id_by_name(c: "httpx.Client", name: str) -> Optional[str]:
    """Resolve a table id by name via the meta API. None if the base has no
    such table (which is fine -- nothing to wipe)."""
    r = c.get(f"{_API}/v0/meta/bases/{AIRTABLE_BASE_ID}/tables", headers=_headers())
    r.raise_for_status()
    for t in r.json().get("tables", []):
        if t.get("name", "").lower() == name.lower():
            return t["id"]
    return None


def _delete_all_in_table(c: "httpx.Client", table_id: str) -> int:
    """Delete every record in a table, 10 ids per request (Airtable's cap)."""
    deleted = 0
    while True:
        r = c.get(f"{_API}/v0/{AIRTABLE_BASE_ID}/{table_id}",
                  headers=_headers(), params={"pageSize": "100", "fields[]": []})
        r.raise_for_status()
        ids = [rec["id"] for rec in r.json().get("records", [])]
        if not ids:
            break
        for i in range(0, len(ids), 10):
            batch = ids[i:i + 10]
            dr = c.delete(f"{_API}/v0/{AIRTABLE_BASE_ID}/{table_id}",
                          headers=_headers(),
                          params=[("records[]", rid) for rid in batch])
            dr.raise_for_status()
            deleted += len(batch)
    return deleted


def reset_customer_data() -> dict:
    """Clear all customer data + client identity for a fresh handoff. Best-effort;
    returns a per-table summary with any errors."""
    if not is_configured():
        return {"ok": False, "detail": "Airtable not configured", "wiped": {}}
    wiped: dict = {}
    errors: dict = {}
    with httpx.Client(timeout=60) as c:
        for name in _CUSTOMER_DATA_TABLES:
            try:
                tid = _table_id_by_name(c, name)
                if not tid:
                    continue
                wiped[name] = _delete_all_in_table(c, tid)
            except Exception as e:
                errors[name] = f"{type(e).__name__}: {e}"
    cleared = []
    for key in _IDENTITY_SETTING_KEYS:
        try:
            set_setting(key, "")
            cleared.append(key)
        except Exception as e:
            errors[f"setting:{key}"] = f"{type(e).__name__}: {e}"
    try:
        global _settings_cache, _settings_cache_at
        _settings_cache = {}
        _settings_cache_at = 0
    except Exception:
        pass
    return {"ok": not errors, "wiped": wiped, "identity_cleared": cleared, "errors": errors}
