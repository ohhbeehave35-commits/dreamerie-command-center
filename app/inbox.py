"""
Client inbox -- how Vinny feeds information INTO a client's own Annabelle.

THE SHAPE OF THE PROBLEM
Every client gets a fully separate deployment: own repo, own Render service,
own Airtable base, own access code. That separation is the product -- one
client's assistant must never see another's data. But it also means "send my
client some product research" has no path: Vinny's app cannot write into a
client's brain, because by design it holds none of their credentials.

Two ways across that boundary:
  A. Vinny's app holds every client's Airtable credentials and writes directly.
     No code needed on their side -- and one leak exposes every client.
  B. Each client app exposes an authenticated RECEIVE endpoint, and Vinny's
     app posts to it. Each client's data stays behind their own boundary and
     their own secret.

This is B. Because every client deployment is a clone of this repo, shipping
the receiver here means every future client has it on day one.

WHAT ARRIVES IS NOT AUTOMATICALLY TRUSTED
Items land marked as sent-by-the-owner, with their source recorded. A client's
assistant quoting "research" it cannot attribute is the fabrication problem
wearing a different hat -- so every item keeps who sent it and when.
"""

import hmac
import logging
import os
from datetime import datetime, timezone

import httpx

from . import crm

log = logging.getLogger(__name__)

INBOX_TABLE = "Inbox"
_inbox_table_id_cache = None

# Shared secret this deployment accepts pushes with. Set per client deployment.
INBOX_SECRET_ENV = "CLIENT_INBOX_SECRET"

KINDS = ("update", "research", "document", "media", "instruction")

_FIELDS = [("Title", "singleLineText"), ("Kind", "singleSelect"),
           ("Body", "multilineText"), ("URL", "singleLineText"),
           ("From", "singleLineText"), ("ReceivedAt", "singleLineText")]


def receive_secret() -> str:
    return os.environ.get(INBOX_SECRET_ENV, "")


def is_receiving() -> bool:
    """A deployment with no secret set does not accept pushes at all."""
    return bool(receive_secret())


def verify_secret(presented: str) -> bool:
    """Constant-time compare. An empty configured secret NEVER matches --
    otherwise a deployment that forgot to set one would accept anything."""
    expected = receive_secret()
    if not expected or not presented:
        return False
    return hmac.compare_digest(presented, expected)


def _ensure_table() -> str:
    global _inbox_table_id_cache
    if _inbox_table_id_cache:
        return _inbox_table_id_cache
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{crm._API}/v0/meta/bases/{crm.AIRTABLE_BASE_ID}/tables",
                  headers=crm._headers())
        r.raise_for_status()
        for t in r.json().get("tables", []):
            if t.get("name", "").lower() == INBOX_TABLE.lower():
                _inbox_table_id_cache = t["id"]
                for _n, _t in _FIELDS:
                    try:
                        crm._ensure_field(c, t["id"], t, _n, _t)
                    except Exception as e:
                        log.warning("INBOX_MIGRATE_SKIP field=%s %s", _n, type(e).__name__)
                return _inbox_table_id_cache
        fields = [{"name": "Title", "type": "singleLineText"},
                  {"name": "Kind", "type": "singleSelect",
                   "options": {"choices": [{"name": k} for k in KINDS]}},
                  {"name": "Body", "type": "multilineText"},
                  {"name": "URL", "type": "singleLineText"},
                  {"name": "From", "type": "singleLineText"},
                  {"name": "ReceivedAt", "type": "singleLineText"}]
        r = c.post(f"{crm._API}/v0/meta/bases/{crm.AIRTABLE_BASE_ID}/tables",
                   headers=crm._headers(), json={"name": INBOX_TABLE, "fields": fields})
        r.raise_for_status()
        _inbox_table_id_cache = r.json()["id"]
        return _inbox_table_id_cache


def store(title: str, body: str = "", kind: str = "update", url: str = "",
          sender: str = "Stinger Industries") -> tuple:
    """Record one received item. Returns (ok, message)."""
    title = (title or "").strip()
    if not title:
        return False, "An inbox item needs a title."
    if kind not in KINDS:
        kind = "update"
    if not crm.is_configured():
        return False, "NOT SAVED -- this deployment's store isn't connected."
    try:
        tid = _ensure_table()
        with httpx.Client(timeout=30) as c:
            r = c.post(f"{crm._API}/v0/{crm.AIRTABLE_BASE_ID}/{tid}", headers=crm._headers(),
                       json={"fields": {
                           "Title": title[:300], "Kind": kind,
                           "Body": (body or "").strip()[:50000],
                           "URL": (url or "").strip()[:1000],
                           # Provenance is not optional: an assistant quoting
                           # something it can't attribute is fabrication.
                           "From": (sender or "unknown")[:200],
                           "ReceivedAt": datetime.now(timezone.utc).isoformat(),
                       }, "typecast": True})
        if r.status_code >= 400:
            body_txt = (r.text or "")[:300]
            log.error("INBOX_SAVE_FAIL title=%s http=%s body=%s",
                      title, r.status_code, body_txt)
            return False, f"NOT SAVED (HTTP {r.status_code}): {body_txt}"
        return True, f"Received: {title}"
    except Exception as e:
        log.exception("INBOX_SAVE_FAIL title=%s", title)
        return False, f"NOT SAVED ({type(e).__name__})."


def unread(limit: int = 20) -> list:
    """Recent inbox items, newest first."""
    if not crm.is_configured():
        return []
    try:
        tid = _ensure_table()
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{crm._API}/v0/{crm.AIRTABLE_BASE_ID}/{tid}",
                      headers=crm._headers(), params={"pageSize": "100"})
            r.raise_for_status()
        recs = r.json().get("records", [])
        recs.sort(key=lambda x: x.get("createdTime", ""), reverse=True)
        return [{"id": rec["id"], "title": f.get("Title", ""), "kind": f.get("Kind", "update"),
                 "body": f.get("Body", ""), "url": f.get("URL", ""),
                 "sender": f.get("From", ""), "received": f.get("ReceivedAt", "")}
                for rec in recs if (f := rec.get("fields", {})).get("Title")][:limit]
    except Exception:
        log.exception("INBOX_READ_FAIL")
        return []


# ---------------------------------------------------------------- sending

def push_to_client(base_url: str, secret: str, title: str, body: str = "",
                   kind: str = "update", url: str = "",
                   sender: str = "Stinger Industries") -> tuple:
    """Send one item to a CLIENT deployment's /api/inbox. Returns (ok, message).

    Never silently succeeds: a push that the client app rejected must say so,
    with its status, or Vinny believes a client received something they didn't.
    """
    base_url = (base_url or "").strip().rstrip("/")
    if not base_url.startswith("https://"):
        return False, "The client URL must be an https:// address."
    if not secret:
        return False, "No shared secret for that client -- nothing was sent."
    if not (title or "").strip():
        return False, "Give the item a title."
    try:
        r = httpx.post(f"{base_url}/api/inbox",
                       headers={"X-Inbox-Secret": secret},
                       json={"title": title, "body": body, "kind": kind,
                             "url": url, "sender": sender},
                       timeout=20)
    except Exception as e:
        return False, (f"Couldn't reach {base_url} ({type(e).__name__}). "
                       f"NOTHING was delivered.")
    if r.status_code == 401:
        return False, ("That client app rejected the shared secret. Nothing was "
                       "delivered -- check CLIENT_INBOX_SECRET on their side.")
    if r.status_code == 404:
        return False, (f"{base_url} has no /api/inbox -- it's probably running an "
                       f"older build. Nothing was delivered.")
    if r.status_code >= 400:
        return False, f"That client app returned HTTP {r.status_code}. Nothing was delivered."
    return True, f"Delivered to {base_url}: {title}"
