"""
Long-term memory for the Command Center agents.

The asset library (assets.py) remembers *media links*. The CRM (crm.py)
remembers *people*. This remembers *facts* -- durable knowledge the agents
should carry between conversations: business strategy, how the owner likes
things done, research findings, standing decisions, "we don't say X", the
name of the good candle supplier. Anything that would otherwise have to be
re-explained every session.

Same Airtable-table-per-feature pattern as assets.py/crm.py: the table is
auto-created on first use, and every call degrades to a graceful "not
connected" string if Airtable isn't configured (never raises).

Tag entries with the business (dreamerie / suzy_d / bear_arms / peptides) so a
fact saved on one tab can be recalled scoped to that tab -- the same tag
vocabulary the events tracker and asset library already use.
"""

import httpx

from . import crm

MEMORY_TABLE = "Agent Memory"

_memory_table_id_cache = None


def _ensure_memory_table() -> str:
    """Return the Agent Memory table id, creating it if needed."""
    global _memory_table_id_cache
    if _memory_table_id_cache:
        return _memory_table_id_cache
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{crm._API}/v0/meta/bases/{crm.AIRTABLE_BASE_ID}/tables", headers=crm._headers())
        r.raise_for_status()
        for t in r.json().get("tables", []):
            if t.get("name", "").lower() == MEMORY_TABLE.lower():
                _memory_table_id_cache = t["id"]
                return _memory_table_id_cache
        # Primary (first) field must be a plain text type in Airtable.
        fields = [
            {"name": "Summary", "type": "singleLineText"},
            {"name": "Content", "type": "multilineText"},
            {"name": "Tags", "type": "singleLineText"},
            {"name": "Source", "type": "singleLineText"},
        ]
        r = c.post(f"{crm._API}/v0/meta/bases/{crm.AIRTABLE_BASE_ID}/tables",
                   headers=crm._headers(), json={"name": MEMORY_TABLE, "fields": fields})
        r.raise_for_status()
        _memory_table_id_cache = r.json()["id"]
        return _memory_table_id_cache


def add_memory_checked(summary: str, content: str = "", tags: str = "",
                       source: str = "") -> tuple:
    """Save one memory. Returns (ok: bool, message: str). Never raises.

    Mirrors assets.add_asset_checked: callers that must KNOW whether the write
    landed (the owner-side push, tests) use this; the string wrapper below is
    for the agent tool where every outcome is prose anyway.
    """
    if not crm.is_configured():
        return False, "Memory isn't available (Airtable not connected)."
    if not summary.strip():
        return False, "I need at least a one-line summary to remember something."
    try:
        tid = _ensure_memory_table()
        with httpx.Client(timeout=30) as c:
            r = c.post(f"{crm._API}/v0/{crm.AIRTABLE_BASE_ID}/{tid}", headers=crm._headers(),
                       json={"fields": {
                           "Summary": summary.strip()[:250],
                           "Content": (content or summary).strip()[:5000],
                           "Tags": tags.strip()[:500],
                           "Source": source.strip()[:200],
                       }, "typecast": True})
            if r.status_code >= 400:
                body = (r.text or "")[:300]
                return False, f"Memory store rejected it (HTTP {r.status_code}): {body}"
        return True, f'Remembered: "{summary.strip()[:120]}".'
    except Exception as e:
        return False, f"Couldn't save that memory: {type(e).__name__}: {e}"


def add_memory(summary: str, content: str = "", tags: str = "", source: str = "") -> str:
    """String-returning wrapper for the save_memory agent tool. Never raises."""
    return add_memory_checked(summary, content, tags, source)[1]


def list_memory_raw(limit: int = 25, tag: str = "") -> list:
    """Return recent memories as dicts for an owner-side view. [] on error."""
    if not crm.is_configured():
        return []
    try:
        tid = _ensure_memory_table()
        params = {
            "pageSize": str(max(1, min(limit, 50))),
            "sort[0][field]": "Summary",
            "sort[0][direction]": "asc",
        }
        if tag.strip():
            t = tag.strip().replace("'", "")
            params["filterByFormula"] = "FIND(LOWER('" + t + "'), LOWER({Tags}))>0"
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{crm._API}/v0/{crm.AIRTABLE_BASE_ID}/{tid}", headers=crm._headers(), params=params)
            r.raise_for_status()
        out = []
        for rec in r.json().get("records", []):
            f = rec.get("fields", {})
            out.append({
                "id": rec.get("id", ""),
                "summary": f.get("Summary", ""),
                "content": f.get("Content", ""),
                "tags": f.get("Tags", ""),
                "source": f.get("Source", ""),
            })
        return out
    except Exception:
        return []


def recall_memory(query: str = "", tag: str = "", limit: int = 8) -> str:
    """Search memory by keyword across summary/content/tags. Returns a short
    list or an explanation -- never raises. The honesty contract matters: a
    store it couldn't reach is NOT the same as nothing being remembered."""
    if not crm.is_configured():
        return ("I couldn't reach memory (Airtable not connected), so I can't "
                "tell you what's been saved. That's different from nothing being saved.")
    try:
        tid = _ensure_memory_table()
        formula_parts = []
        if query.strip():
            q = query.strip().replace("'", "")
            formula_parts.append(
                "OR(FIND(LOWER('" + q + "'), LOWER({Summary}))>0, "
                "FIND(LOWER('" + q + "'), LOWER({Content}))>0, "
                "FIND(LOWER('" + q + "'), LOWER({Tags}))>0)"
            )
        if tag.strip():
            t = tag.strip().replace("'", "")
            formula_parts.append("FIND(LOWER('" + t + "'), LOWER({Tags}))>0")
        params = {"pageSize": str(max(1, min(limit, 25)))}
        if formula_parts:
            params["filterByFormula"] = "AND(" + ",".join(formula_parts) + ")" if len(formula_parts) > 1 else formula_parts[0]
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{crm._API}/v0/{crm.AIRTABLE_BASE_ID}/{tid}", headers=crm._headers(), params=params)
            r.raise_for_status()
        recs = r.json().get("records", [])
        if not recs:
            return "Nothing in memory matches that yet."
        lines = []
        for rec in recs:
            f = rec.get("fields", {})
            tag_str = f" [{f['Tags']}]" if f.get("Tags") else ""
            body = f.get("Content") or f.get("Summary", "")
            lines.append(f"- {f.get('Summary','?')}{tag_str}: {body}")
        return "\n".join(lines)
    except Exception as e:
        return f"Couldn't search memory: {type(e).__name__}: {e}"
