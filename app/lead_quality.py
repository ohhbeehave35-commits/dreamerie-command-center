"""Duplicate management and contact scoring for the Leads table.

Pure functions, no network. crm.py feeds them the Airtable records and applies
the result; Annabelle's find_duplicate_leads, score_leads and
merge_duplicate_leads tools and the owner JSON routes all use crm.py.

Scoring is plain and explainable: every point has a reason in words, so the
owner can see why a lead is hot, warm or cold. Nothing here calls a model.
"""

from __future__ import annotations

import re
from typing import Dict, List

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Points for what we know and can act on.
_POINTS = (
    ("Name", 5, "has a name"),
    ("Business", 5, "knows which business it is for"),
    ("Request", 15, "says what they want"),
    ("Notes", 5, "has notes"),
)
_STATUS_POINTS = {"New": 0, "Contacted": 10, "Quoted": 15, "Scheduled": 25, "Done": 25}
_LOST_CAP = 10
HOT_AT = 70
WARM_AT = 40


def clean_email(value) -> str:
    v = str(value or "").strip().lower()
    return v if _EMAIL_RE.match(v) else ""


def phone_digits(value) -> str:
    """Last ten digits of a phone number, or '' when there are fewer than seven."""
    d = re.sub(r"\D", "", str(value or ""))
    if len(d) < 7:
        return ""
    return d[-10:]


def clean_name(value) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", str(value or "").lower())).strip()


def score_lead(fields: dict) -> dict:
    """Score one lead record (Airtable field names) from 0 to 100."""
    f = fields or {}
    score = 0
    reasons: List[str] = []
    if clean_email(f.get("Email")):
        score += 20
        reasons.append("+20 has a working-looking email")
    if phone_digits(f.get("Phone")):
        score += 20
        reasons.append("+20 has a phone number")
    for key, pts, why in _POINTS:
        if str(f.get(key) or "").strip():
            score += pts
            reasons.append(f"+{pts} {why}")
    if f.get("SMS Opt-In"):
        score += 5
        reasons.append("+5 agreed to text messages")
    status = str(f.get("Status") or "New").strip() or "New"
    if status == "Lost":
        if score > _LOST_CAP:
            reasons.append(f"capped at {_LOST_CAP}: marked Lost")
        score = min(score, _LOST_CAP)
    else:
        pts = _STATUS_POINTS.get(status, 0)
        if pts:
            score += pts
            reasons.append(f"+{pts} status is {status}")
    score = max(0, min(100, score))
    label = "hot" if score >= HOT_AT else "warm" if score >= WARM_AT else "cold"
    return {"score": score, "label": label, "reasons": reasons}


def find_duplicate_groups(leads: List[dict]) -> List[dict]:
    """Group leads that are the same person.

    leads: dicts with id, name, email, phone, business. Two leads match on the
    same email, the same phone (last ten digits), or the same name inside the
    same business. Blank values never match. Returns [{"ids": [...], "reason": str}].
    """
    parent: Dict[str, str] = {}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    keys: Dict[tuple, List[str]] = {}
    for lead in leads:
        lid = str(lead.get("id") or "")
        if not lid:
            continue
        parent.setdefault(lid, lid)
        email = clean_email(lead.get("email"))
        phone = phone_digits(lead.get("phone"))
        name = clean_name(lead.get("name"))
        business = str(lead.get("business") or "").strip().lower()
        if email:
            keys.setdefault(("email", email), []).append(lid)
        if phone:
            keys.setdefault(("phone", phone), []).append(lid)
        if name and business:
            keys.setdefault(("name", name, business), []).append(lid)

    reason_for: Dict[str, set] = {}
    for key, ids in keys.items():
        if len(ids) < 2:
            continue
        for other in ids[1:]:
            union(ids[0], other)
        for i in ids:
            reason_for.setdefault(i, set()).add(key[0])

    groups: Dict[str, List[str]] = {}
    for lid in parent:
        groups.setdefault(find(lid), []).append(lid)
    out = []
    for ids in groups.values():
        if len(ids) < 2:
            continue
        why = sorted({r for i in ids for r in reason_for.get(i, ())})
        out.append({"ids": ids, "reason": "same " + " and ".join(why)})
    return out


def merge_fields(keep: dict, others: List[dict]) -> dict:
    """The record the keeper becomes: its own values win, blanks are filled from
    the others in order, notes are joined, a text opt-in is kept if anyone had it."""
    merged = dict(keep or {})
    for o in others:
        for k, v in (o or {}).items():
            if k == "Notes":
                continue
            if k == "SMS Opt-In":
                if v:
                    merged[k] = True
                continue
            if v not in (None, "") and merged.get(k) in (None, ""):
                merged[k] = v
    notes: List[str] = []
    for rec in [keep or {}] + list(others):
        n = str((rec or {}).get("Notes") or "").strip()
        if n and n not in notes:
            notes.append(n)
    if notes:
        merged["Notes"] = "\n---\n".join(notes)
    return merged
