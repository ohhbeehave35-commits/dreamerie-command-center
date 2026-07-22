"""
Airtable-backed Business Events tracker for Susan's Command Center.

One shared table covering BOTH sides of her business, tagged by which one
each event belongs to:
- "The Dreamerie" -- shop pop-ups, markets, craft fairs, vendor booths
- "Suzy D / TikTok" -- livestream collabs, brand deals, shoutout swaps
- "Both" -- things that touch both sides (e.g. a market appearance she also
  streams live from)

Lets Susan (and the assistant) cross-reference what's coming up across both
identities in one place instead of two disconnected mental lists.
"""

import os

import httpx

from . import crm

_API = "https://api.airtable.com"
TABLE_NAME = os.environ.get("AIRTABLE_EVENTS_TABLE", "Business Events")
_table_id_cache = None

BUSINESS_CHOICES = ["The Dreamerie", "Suzy D / TikTok", "Both"]
STATUS_CHOICES = ["Idea", "Tentative", "Confirmed", "Done", "Cancelled"]

_FIELDS = [
    {"name": "Event", "type": "singleLineText"},
    {"name": "Business", "type": "singleSelect", "options": {"choices": [{"name": c} for c in BUSINESS_CHOICES]}},
    {"name": "Date", "type": "singleLineText"},
    {"name": "Time", "type": "singleLineText"},
    {"name": "Location", "type": "multilineText"},
    {"name": "Status", "type": "singleSelect", "options": {"choices": [{"name": c} for c in STATUS_CHOICES]}},
    {"name": "Notes", "type": "multilineText"},
]


def is_configured() -> bool:
    return crm.is_configured()


def _ensure_table() -> str:
    global _table_id_cache
    if _table_id_cache:
        return _table_id_cache
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{_API}/v0/meta/bases/{crm.AIRTABLE_BASE_ID}/tables", headers=crm._headers())
        r.raise_for_status()
        for t in r.json().get("tables", []):
            if t.get("name", "").lower() == TABLE_NAME.lower():
                _table_id_cache = t["id"]
                return _table_id_cache
        r = c.post(
            f"{_API}/v0/meta/bases/{crm.AIRTABLE_BASE_ID}/tables",
            headers=crm._headers(),
            json={"name": TABLE_NAME, "fields": _FIELDS},
        )
        r.raise_for_status()
        _table_id_cache = r.json()["id"]
        return _table_id_cache


def _fetch_all(business: str = "") -> list:
    tid = _ensure_table()
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{_API}/v0/{crm.AIRTABLE_BASE_ID}/{tid}", headers=crm._headers(), params={"pageSize": "100"})
        r.raise_for_status()
    recs = [rec["fields"] for rec in r.json().get("records", [])]
    if business and business != "All":
        recs = [f for f in recs if f.get("Business", "") == business]
    return recs


def list_events_raw(business: str = "") -> list:
    """UI-facing: structured list of events, optionally filtered by business."""
    if not is_configured():
        return []
    try:
        return _fetch_all(business)
    except Exception:
        return []


def list_events(business: str = "", search: str = "") -> str:
    """Assistant-facing: text summary of events, optionally filtered."""
    if not is_configured():
        return "The events tracker isn't connected yet."
    try:
        recs = _fetch_all(business)
        if search:
            s = search.lower()
            recs = [f for f in recs if s in f.get("Event", "").lower() or s in f.get("Location", "").lower()]
        if not recs:
            return "No matching events found."
        lines = []
        for f in recs:
            lines.append(
                f"[{f.get('Business', '')}] {f.get('Event', '')} -- {f.get('Date', '')} {f.get('Time', '')} "
                f"@ {f.get('Location', '')} | {f.get('Status', '')}"
                + (f" | {f.get('Notes', '')}" if f.get("Notes") else "")
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Couldn't read the events tracker ({type(e).__name__})."


def add_event(event="", business="Both", date="", time="", location="", status="Idea", notes="") -> str:
    """Assistant-facing: log a new event under either or both identities."""
    if not event:
        return "I need at least the event name."
    if not is_configured():
        return f"Noted this event: {event}. (The events tracker isn't connected yet.)"
    try:
        tid = _ensure_table()
        fields = {"Event": event, "Business": business, "Date": date, "Time": time,
                  "Location": location, "Status": status, "Notes": notes}
        with httpx.Client(timeout=30) as c:
            c.post(f"{_API}/v0/{crm.AIRTABLE_BASE_ID}/{tid}", headers=crm._headers(),
                   json={"fields": fields, "typecast": True})
        return f"Logged event: {event} ({business}) on {date}."
    except Exception as e:
        return f"Couldn't log that event ({type(e).__name__}), but noted: {event}"
