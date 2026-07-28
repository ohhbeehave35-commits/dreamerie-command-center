"""
Google Calendar integration for scheduling removals and checking availability.

Handles OAuth token storage/refresh in Airtable Settings, queries calendar for
existing removals, and creates new removal events. Public widget gets read-only
access (see availability); owner gets full read-write.
"""

import os
import json
from datetime import datetime, timedelta
from typing import Optional
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from . import crm

# Google OAuth config
CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "http://127.0.0.1:8040/auth/google-callback")
SCOPES = ["https://www.googleapis.com/auth/calendar"]

CALENDAR_ID_KEY = "google_calendar_id"
GOOGLE_TOKEN_KEY = "google_oauth_token"


def _parse_time(time_str: str) -> tuple[int, int]:
    """Parse common AI time formats into (hour, minute) on a 24-hour clock.
    Handles '9 AM', '9:30 AM', '14:00', '14', '2pm'."""
    s = time_str.strip().upper()
    for fmt in ("%I:%M %p", "%I:%M%p", "%I %p", "%I%p", "%H:%M", "%H"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.hour, dt.minute
        except ValueError:
            continue
    raise ValueError(f"Cannot parse time {time_str!r} — expected '9 AM', '14:00', etc.")


def get_oauth_flow():
    """Create an OAuth flow for authorization.

    This is a confidential "web" application client (authenticates with the
    client secret), so PKCE is disabled: connect and callback run in separate
    requests with separate Flow objects, and a PKCE code_verifier generated in
    the connect step would not survive to the callback -- causing the token
    exchange to fail. With autogenerate_code_verifier=False, no code_challenge
    is sent and the secret alone authenticates the exchange.
    """
    if not CLIENT_ID or not CLIENT_SECRET:
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


def is_configured() -> bool:
    """Check if Google Calendar is connected."""
    return bool(crm.get_setting(GOOGLE_TOKEN_KEY, ""))


def get_credentials() -> Optional[Credentials]:
    """Retrieve stored OAuth credentials, refresh if needed."""
    token_json = crm.get_setting(GOOGLE_TOKEN_KEY, "")
    if not token_json:
        return None
    try:
        token_data = json.loads(token_json)
        creds = Credentials.from_authorized_user_info(token_data, SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            crm.set_setting(GOOGLE_TOKEN_KEY, creds.to_json())
        return creds
    except Exception:
        return None


def get_calendar_service():
    """Get Google Calendar service if authenticated."""
    creds = get_credentials()
    if not creds:
        return None
    try:
        return build("calendar", "v3", credentials=creds)
    except Exception:
        return None


def store_token(token_json: str):
    """Store OAuth token in Airtable Settings."""
    crm.set_setting(GOOGLE_TOKEN_KEY, token_json)


def check_availability(date_str: str) -> dict:
    """
    Check if a removal is already scheduled on a given date.
    Returns {"available": bool, "removals": [{"time": "9am-12pm", "area": "Downtown"}]}
    """
    service = get_calendar_service()
    if not service:
        return {"available": True, "removals": [], "reason": "Calendar not connected"}

    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        start = date_obj.replace(hour=0, minute=0, second=0).isoformat() + "Z"
        end = (date_obj + timedelta(days=1)).replace(hour=0, minute=0, second=0).isoformat() + "Z"

        calendar_id = crm.get_setting(CALENDAR_ID_KEY, "primary")
        events = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=start,
                timeMax=end,
                q="removal",  # Only events with "removal" in title
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )

        removals = []
        for event in events.get("items", []):
            title = event.get("summary", "")
            start_time = event.get("start", {}).get("dateTime", "")
            if start_time:
                dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                time_str = dt.strftime("%I:%M %p")
                # Extract area from description or title (e.g., "Downtown removal")
                area = title.split()[0] if title else "Unknown"
                removals.append({"time": time_str, "area": area})

        return {
            "available": len(removals) < 2,  # Only 2 removals per day max
            "removals": removals,
        }
    except Exception as e:
        return {"available": True, "removals": [], "reason": str(e)}


def list_upcoming_events(days: int = 30) -> dict:
    """
    List events on the connected calendar over the next `days` days, for the
    Command Center's Schedule panel. Returns
    {"connected": bool, "events": [{"date": "YYYY-MM-DD", "time": "9:00 AM",
    "title": "...", "kind": "removal"|"inspection"|"other"}]}
    """
    if not is_configured():
        return {"connected": False, "events": []}

    service = get_calendar_service()
    if not service:
        return {"connected": False, "events": []}

    try:
        now = datetime.utcnow()
        start = now.isoformat() + "Z"
        end = (now + timedelta(days=days)).isoformat() + "Z"
        calendar_id = crm.get_setting(CALENDAR_ID_KEY, "primary")
        events = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=start,
                timeMax=end,
                singleEvents=True,
                orderBy="startTime",
                maxResults=100,
            )
            .execute()
        )

        out = []
        for event in events.get("items", []):
            title = event.get("summary", "(untitled)")
            start_info = event.get("start", {})
            dt_str = start_info.get("dateTime")
            all_day = start_info.get("date")
            if dt_str:
                dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00")).astimezone()
                date_val = dt.strftime("%Y-%m-%d")
                time_val = dt.strftime("%I:%M %p").lstrip("0")
            elif all_day:
                date_val = all_day
                time_val = "All day"
            else:
                continue

            lower_title = title.lower()
            if "removal" in lower_title:
                kind = "removal"
            elif "inspection" in lower_title or "follow-up" in lower_title:
                kind = "inspection"
            else:
                kind = "other"

            out.append({"date": date_val, "time": time_val, "title": title, "kind": kind})

        return {"connected": True, "events": out}
    except Exception as e:
        return {"connected": True, "events": [], "reason": str(e)}


def create_removal_event(date_str: str, area: str, time_str: str, customer_name: str = "", customer_phone: str = "") -> str:
    """
    Create a removal event on the calendar.
    Returns confirmation message or error.
    """
    service = get_calendar_service()
    if not service:
        return "Calendar not connected — connect from Settings panel."

    try:
        # Parse date and time
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        hour, minute = _parse_time(time_str)
        start_dt = date_obj.replace(hour=hour, minute=minute)
        end_dt = start_dt + timedelta(hours=3)  # 3-hour removal window

        event = {
            "summary": f"{area} removal",
            "description": f"Honey bee removal\nCustomer: {customer_name or 'TBD'}\nPhone: {customer_phone or 'TBD'}",
            "start": {"dateTime": start_dt.isoformat(), "timeZone": "America/New_York"},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": "America/New_York"},
        }

        calendar_id = crm.get_setting(CALENDAR_ID_KEY, "primary")
        service.events().insert(calendarId=calendar_id, body=event).execute()
        return f"Removal scheduled for {date_str} at {time_str} in {area}."
    except Exception as e:
        return f"Couldn't create event: {type(e).__name__}: {e}"


def create_inspection_event(date_str: str, area: str, time_str: str, visit_type: str,
                             customer_name: str = "", customer_phone: str = "") -> str:
    """
    Create a paid Inspection Service Call on the calendar -- separate from a
    removal event and not counted against the 2-removals/day cap (check_availability
    only searches for "removal" in the title, so these never collide with it).

    visit_type: "new_inspection" ($89) or "past_customer_followup" ($49).
    Returns confirmation message or error.
    """
    service = get_calendar_service()
    if not service:
        return "Calendar not connected — connect from Settings panel."

    is_followup = visit_type == "past_customer_followup"
    label = "follow-up" if is_followup else "inspection"
    fee = 49 if is_followup else 89

    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        hour, minute = _parse_time(time_str)
        start_dt = date_obj.replace(hour=hour, minute=minute)
        end_dt = start_dt + timedelta(hours=1)  # shorter than a full removal visit

        kind = "Past-customer follow-up" if is_followup else "New-customer inspection"
        event = {
            "summary": f"{area} {label} (${fee})",
            "description": f"{kind}\nFee: ${fee}\nCustomer: {customer_name or 'TBD'}\nPhone: {customer_phone or 'TBD'}",
            "start": {"dateTime": start_dt.isoformat(), "timeZone": "America/New_York"},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": "America/New_York"},
        }

        calendar_id = crm.get_setting(CALENDAR_ID_KEY, "primary")
        service.events().insert(calendarId=calendar_id, body=event).execute()
        return f"{kind} scheduled for {date_str} at {time_str} in {area} (${fee})."
    except Exception as e:
        return f"Couldn't create event: {type(e).__name__}: {e}"
