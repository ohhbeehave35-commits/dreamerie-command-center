"""
Results / proof-of-value summary for the owner dashboard.

Answers the question a prospective client actually cares about: "what has
this thing done for the business lately?" Pulls straight from the Leads and
Conversations tables already in Airtable -- no new schema, no computed
fields (lesson learned the hard way building the login system: use Airtable's
built-in per-record `createdTime` metadata for date filtering, never a
computed field type in the schema itself).
"""

from datetime import datetime, timezone

import httpx

from . import crm

BOOKED_STATUSES = {"Scheduled", "Done"}
LOST_STATUSES = {"Lost"}


def _month_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m")


def _fetch_all_records(table_id: str) -> list:
    """Fetch every record (fields + createdTime) from a table, paginating."""
    records = []
    offset = None
    with httpx.Client(timeout=30) as c:
        while True:
            params = {"pageSize": "100"}
            if offset:
                params["offset"] = offset
            r = c.get(f"{crm._API}/v0/{crm.AIRTABLE_BASE_ID}/{table_id}",
                      headers=crm._headers(), params=params)
            r.raise_for_status()
            j = r.json()
            records.extend(j.get("records", []))
            offset = j.get("offset")
            if not offset:
                break
    return records


def get_monthly_summary(month: str = "") -> dict:
    """Returns lead/conversation stats for one calendar month (default: current).

    Shape:
        {month, leads_total, leads_booked, leads_new, leads_lost,
         by_source: {...}, by_status: {...}, conversations_total,
         customer_messages, leads_all_time, booked_all_time}
    """
    now = datetime.now(timezone.utc)
    target_month = month or _month_key(now)

    empty = {
        "month": target_month, "leads_total": 0, "leads_booked": 0,
        "leads_new": 0, "leads_lost": 0, "by_source": {}, "by_status": {},
        "conversations_total": 0, "customer_messages": 0,
        "leads_all_time": 0, "booked_all_time": 0,
    }
    if not crm.is_configured():
        return empty

    try:
        leads_tid = crm._ensure_table()
        leads = _fetch_all_records(leads_tid)
    except Exception:
        leads = []

    try:
        conv_tid = crm._ensure_conv_table()
        convs = _fetch_all_records(conv_tid)
    except Exception:
        convs = []

    leads_this_month = [r for r in leads if r.get("createdTime", "").startswith(target_month)]
    convs_this_month = [r for r in convs if r.get("createdTime", "").startswith(target_month)]

    by_source, by_status = {}, {}
    booked = new = lost = 0
    for r in leads_this_month:
        f = r.get("fields", {})
        status = f.get("Status", "Unknown")
        source = f.get("Source", "Unknown")
        by_status[status] = by_status.get(status, 0) + 1
        by_source[source] = by_source.get(source, 0) + 1
        if status in BOOKED_STATUSES:
            booked += 1
        elif status == "New":
            new += 1
        elif status in LOST_STATUSES:
            lost += 1

    booked_all_time = sum(1 for r in leads if r.get("fields", {}).get("Status") in BOOKED_STATUSES)
    customer_messages = sum(1 for r in convs_this_month if r.get("fields", {}).get("Role") == "user")

    return {
        "month": target_month,
        "leads_total": len(leads_this_month),
        "leads_booked": booked,
        "leads_new": new,
        "leads_lost": lost,
        "by_source": by_source,
        "by_status": by_status,
        "conversations_total": len(convs_this_month),
        "customer_messages": customer_messages,
        "leads_all_time": len(leads),
        "booked_all_time": booked_all_time,
    }
