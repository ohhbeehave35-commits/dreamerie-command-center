"""
HubSpot CRM integration for Stinger Industries Command Center.

Env vars required (set in Render):
    HUBSPOT_ACCESS_TOKEN  -- Private App access token from HubSpot

Usage:
    from . import hubspot
    contact = hubspot.create_or_update_contact(name="Maria Rodriguez",
                                               email="maria@email.com",
                                               phone="772-555-0100")

API notes:
    All calls use HubSpot CRM API v3/v4. The old engagements v1 endpoint
    was deprecated in 2023 and returns 410 — do not use it.
    Notes  → POST /crm/v3/objects/notes
    Calls  → POST /crm/v3/objects/calls
    Assocs → PUT  /crm/v4/objects/{from}/{id}/associations/{to}/{id}
"""

import os
import httpx
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_BASE = "https://api.hubapi.com"

_TITLES = {"dr", "mr", "ms", "mrs", "prof", "rev", "capt", "sir"}

# HubSpot v4 association type IDs (HUBSPOT_DEFINED category)
_ASSOC = {
    ("contacts", "deals"):  4,
    ("deals",    "contacts"): 3,
    ("notes",    "contacts"): 202,
    ("notes",    "deals"):    214,
    ("notes",    "companies"): 190,
    ("calls",    "contacts"): 194,
    ("calls",    "deals"):    206,
}


def _split_name(full: str) -> tuple[str, str]:
    parts = full.strip().split()
    if len(parts) >= 2 and parts[0].rstrip(".").lower() in _TITLES:
        parts = parts[1:]
    first = parts[0] if parts else full.strip()
    last = " ".join(parts[1:]) if len(parts) > 1 else ""
    return first, last


def _headers() -> dict:
    token = os.environ.get("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise RuntimeError(
            "HubSpot not configured. Set HUBSPOT_ACCESS_TOKEN in Render environment variables. "
            "Get your token: https://app.hubspot.com/l/workspace-settings/integrations/api-keys (Private App API key)"
        )
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _now_ms() -> str:
    """ISO 8601 timestamp with milliseconds, as HubSpot hs_timestamp expects."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ── ASSOCIATIONS ──────────────────────────────────────────────────────────────

def _associate(from_type: str, from_id: str, to_type: str, to_id: str) -> None:
    """Associate two CRM objects using the v4 associations API."""
    type_id = _ASSOC.get((from_type, to_type))
    if type_id is None:
        log.warning("No known association type for %s → %s", from_type, to_type)
        return
    httpx.put(
        f"{_BASE}/crm/v4/objects/{from_type}/{from_id}/associations/{to_type}/{to_id}",
        headers=_headers(),
        json=[{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": type_id}],
        timeout=10,
    ).raise_for_status()


# ── CONTACTS ─────────────────────────────────────────────────────────────────

def search_contacts(query: str, limit: int = 5) -> list[dict]:
    """
    Search contacts by name, email, phone, or company.
    Returns a list of simplified contact dicts.
    """
    resp = httpx.post(
        f"{_BASE}/crm/v3/objects/contacts/search",
        headers=_headers(),
        json={
            "query": query,
            "properties": ["firstname", "lastname", "email", "phone", "company",
                           "hs_lead_status", "hs_object_id"],
            "limit": limit,
        },
        timeout=10,
    )
    resp.raise_for_status()
    results = []
    for r in resp.json().get("results", []):
        p = r.get("properties", {})
        results.append({
            "id": r["id"],
            "name": f"{p.get('firstname', '')} {p.get('lastname', '')}".strip(),
            "email": p.get("email", ""),
            "phone": p.get("phone", ""),
            "company": p.get("company", ""),
            "status": p.get("hs_lead_status", ""),
        })
    return results


def _search_contact_by_email(email: str) -> dict | None:
    resp = httpx.post(
        f"{_BASE}/crm/v3/objects/contacts/search",
        headers=_headers(),
        json={
            "filterGroups": [{"filters": [
                {"propertyName": "email", "operator": "EQ", "value": email}
            ]}],
            "properties": ["firstname", "lastname", "phone", "email", "hs_object_id"],
            "limit": 1,
        },
        timeout=10,
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


def create_or_update_contact(
    name: str = "",
    email: str = "",
    phone: str = "",
    company: str = "",
    notes: str = "",
) -> dict:
    """
    Create a new HubSpot contact or update the existing one if the email
    already exists. Returns the HubSpot contact record.
    """
    firstname, lastname = _split_name(name)

    props = {k: v for k, v in {
        "firstname": firstname,
        "lastname":  lastname,
        "email":     email,
        "phone":     phone,
        "company":   company,
        "hs_lead_status": "NEW",
    }.items() if v}

    if email:
        existing = _search_contact_by_email(email)
        if existing:
            contact_id = existing["id"]
            resp = httpx.patch(
                f"{_BASE}/crm/v3/objects/contacts/{contact_id}",
                headers=_headers(),
                json={"properties": props},
                timeout=10,
            )
            resp.raise_for_status()
            if notes:
                _add_note(contact_id, "contacts", notes)
            return resp.json()

    resp = httpx.post(
        f"{_BASE}/crm/v3/objects/contacts",
        headers=_headers(),
        json={"properties": props},
        timeout=10,
    )
    resp.raise_for_status()
    record = resp.json()
    if notes:
        _add_note(record["id"], "contacts", notes)
    return record


def update_contact(contact_id: str, **props) -> dict:
    """Update any properties on an existing contact by ID."""
    clean = {k: v for k, v in props.items() if v is not None}
    resp = httpx.patch(
        f"{_BASE}/crm/v3/objects/contacts/{contact_id}",
        headers=_headers(),
        json={"properties": clean},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


# ── DEALS ─────────────────────────────────────────────────────────────────────

def get_deals(limit: int = 10, stage: str = "") -> list[dict]:
    """List open deals, optionally filtered by stage."""
    filters = []
    if stage:
        filters.append({"propertyName": "dealstage", "operator": "EQ", "value": stage})
    body: dict = {
        "properties": ["dealname", "dealstage", "amount", "closedate", "pipeline"],
        "limit": limit,
        "sorts": [{"propertyName": "hs_lastmodifieddate", "direction": "DESCENDING"}],
    }
    if filters:
        body["filterGroups"] = [{"filters": filters}]
    resp = httpx.post(
        f"{_BASE}/crm/v3/objects/deals/search",
        headers=_headers(),
        json=body,
        timeout=10,
    )
    resp.raise_for_status()
    results = []
    for r in resp.json().get("results", []):
        p = r.get("properties", {})
        results.append({
            "id": r["id"],
            "name": p.get("dealname", ""),
            "stage": p.get("dealstage", ""),
            "amount": p.get("amount", ""),
            "close_date": p.get("closedate", ""),
        })
    return results


def create_deal(
    name: str,
    amount: float | None = None,
    stage: str = "",
    contact_id: str | None = None,
    pipeline: str = "default",
) -> dict:
    """
    Create a HubSpot deal and optionally associate it with a contact.
    `stage` must match one of the pipeline stages in the HubSpot account.
    """
    from . import crm as _crm
    if not stage:
        stage = _crm.get_setting("hubspot_default_deal_stage", "appointmentscheduled")
    props: dict = {"dealname": name, "pipeline": pipeline, "dealstage": stage}
    if amount is not None:
        props["amount"] = str(amount)

    resp = httpx.post(
        f"{_BASE}/crm/v3/objects/deals",
        headers=_headers(),
        json={"properties": props},
        timeout=10,
    )
    resp.raise_for_status()
    deal = resp.json()

    if contact_id:
        _associate("deals", deal["id"], "contacts", contact_id)

    return deal


def update_deal_stage(deal_id: str, stage: str) -> dict:
    """Move a deal to a different pipeline stage."""
    resp = httpx.patch(
        f"{_BASE}/crm/v3/objects/deals/{deal_id}",
        headers=_headers(),
        json={"properties": {"dealstage": stage}},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


# ── NOTES ─────────────────────────────────────────────────────────────────────

def _add_note(object_id: str, object_type: str, body: str) -> dict:
    """
    Attach a Note to any CRM object using the v3 Notes API.
    All notes are labeled as AI-generated so CRM readers can distinguish them
    from human-written notes.
    """
    labeled_body = (
        f"[AI-GENERATED NOTE · {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC]\n"
        "[Review before acting on this information.]\n\n"
        + body
    )
    resp = httpx.post(
        f"{_BASE}/crm/v3/objects/notes",
        headers=_headers(),
        json={
            "properties": {
                "hs_timestamp": _now_ms(),
                "hs_note_body": labeled_body,
            }
        },
        timeout=10,
    )
    resp.raise_for_status()
    note = resp.json()
    _associate("notes", note["id"], object_type, object_id)
    return note


# ── CALLS ─────────────────────────────────────────────────────────────────────

def log_call(
    contact_id: str,
    title: str,
    body: str,
    duration_ms: int = 0,
    direction: str = "INBOUND",
) -> dict:
    """Log a phone call engagement on a contact using the v3 Calls API."""
    resp = httpx.post(
        f"{_BASE}/crm/v3/objects/calls",
        headers=_headers(),
        json={
            "properties": {
                "hs_timestamp":      _now_ms(),
                "hs_call_title":     title,
                "hs_call_body":      body,
                "hs_call_duration":  str(duration_ms),
                "hs_call_direction": direction,
                "hs_call_status":    "COMPLETED",
            }
        },
        timeout=10,
    )
    resp.raise_for_status()
    call = resp.json()
    _associate("calls", call["id"], "contacts", contact_id)
    return call


# ── CONVENIENCE: full lead capture ────────────────────────────────────────────

def capture_lead(
    name: str,
    email: str = "",
    phone: str = "",
    company: str = "",
    service: str = "",
    notes: str = "",
    deal_amount: float | None = None,
    deal_stage: str = "",
) -> dict:
    """
    One-call shortcut used by Annabelle after qualifying a lead.
    Creates/updates the contact, opens a deal, and attaches a note.
    Returns {"contact": ..., "deal": ...}.
    """
    contact = create_or_update_contact(name=name, email=email, phone=phone,
                                       company=company, notes=notes)
    deal_name = f"{name} — {service}" if service else f"{name} — New Lead"
    deal = create_deal(name=deal_name, amount=deal_amount, stage=deal_stage,
                       contact_id=contact["id"])
    return {"contact": contact, "deal": deal}
