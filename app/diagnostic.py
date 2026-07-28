"""System diagnostic probes for the Command Center.

Each probe returns:
    {
        "name":       "Stripe",
        "configured": bool,            # env / module set up
        "ok":         True|False|None, # None = configured but not actively probed
        "latency_ms": int|None,
        "error":      str|None,        # short reason on failure
        "hint":       str|None,        # what to check to fix it
    }

`run_all()` returns a summary + list of services. Called by:
  - GET /api/diagnostic
  - the `run_diagnostic` tool Annabelle can invoke
"""

import os
import time
import httpx

from . import crm
from . import emailer
from . import calendar as gcal
from . import social
from . import voice_eleven
from . import stripe_billing
from . import sms
from . import media_gen
from . import files_dropbox
from . import files_gdrive

TIMEOUT = 4.0  # seconds per network probe


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def _probe_anthropic() -> dict:
    ok = bool(os.environ.get("ANTHROPIC_API_KEY"))
    return {
        "name": "Anthropic (Claude)",
        "configured": ok,
        "ok": True if ok else None,
        "latency_ms": 0 if ok else None,
        "error": None,
        "hint": None if ok else "Set ANTHROPIC_API_KEY in Render env.",
    }


def _probe_airtable() -> dict:
    if not crm.is_configured():
        return {
            "name": "Airtable CRM",
            "configured": False, "ok": None, "latency_ms": None, "error": None,
            "hint": "Set AIRTABLE_API_KEY and AIRTABLE_BASE_ID.",
        }
    t0 = time.perf_counter()
    try:
        crm.get_leads_raw(limit=1)
        return {"name": "Airtable CRM", "configured": True, "ok": True,
                "latency_ms": _ms(t0), "error": None, "hint": None}
    except Exception as e:
        return {"name": "Airtable CRM", "configured": True, "ok": False,
                "latency_ms": _ms(t0), "error": f"{type(e).__name__}: {e}"[:200],
                "hint": "Check API key, base id, and table names."}


def _probe_stripe() -> dict:
    if not stripe_billing.is_configured():
        return {
            "name": "Stripe",
            "configured": False, "ok": None, "latency_ms": None, "error": None,
            "hint": "Set STRIPE_SECRET_KEY.",
        }
    t0 = time.perf_counter()
    key = os.environ["STRIPE_SECRET_KEY"]
    try:
        r = httpx.get(
            "https://api.stripe.com/v1/balance",
            headers={"Authorization": f"Bearer {key}"},
            timeout=TIMEOUT,
        )
        ok = r.status_code == 200
        return {"name": "Stripe", "configured": True, "ok": ok,
                "latency_ms": _ms(t0),
                "error": None if ok else f"HTTP {r.status_code}: {r.text[:120]}",
                "hint": None if ok else "Verify STRIPE_SECRET_KEY is valid + not rotated."}
    except Exception as e:
        return {"name": "Stripe", "configured": True, "ok": False,
                "latency_ms": _ms(t0), "error": f"{type(e).__name__}: {e}"[:200],
                "hint": "Stripe API unreachable — check network + key."}


def _probe_hubspot() -> dict:
    token = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
    if not token:
        return {"name": "HubSpot", "configured": False, "ok": None,
                "latency_ms": None, "error": None,
                "hint": "Set HUBSPOT_ACCESS_TOKEN (Private App token)."}
    t0 = time.perf_counter()
    try:
        r = httpx.get(
            "https://api.hubapi.com/crm/v3/objects/contacts?limit=1",
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT,
        )
        ok = r.status_code == 200
        return {"name": "HubSpot", "configured": True, "ok": ok,
                "latency_ms": _ms(t0),
                "error": None if ok else f"HTTP {r.status_code}: {r.text[:120]}",
                "hint": None if ok else "Regenerate the Private App token or check scopes."}
    except Exception as e:
        return {"name": "HubSpot", "configured": True, "ok": False,
                "latency_ms": _ms(t0), "error": f"{type(e).__name__}: {e}"[:200],
                "hint": "HubSpot API unreachable."}


def _probe_twilio() -> dict:
    if not sms.is_configured():
        return {"name": "Twilio SMS", "configured": False, "ok": None,
                "latency_ms": None, "error": None,
                "hint": "Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM."}
    t0 = time.perf_counter()
    sid = os.environ["TWILIO_ACCOUNT_SID"]
    tok = os.environ["TWILIO_AUTH_TOKEN"]
    try:
        r = httpx.get(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}.json",
            auth=(sid, tok), timeout=TIMEOUT,
        )
        ok = r.status_code == 200
        return {"name": "Twilio SMS", "configured": True, "ok": ok,
                "latency_ms": _ms(t0),
                "error": None if ok else f"HTTP {r.status_code}",
                "hint": None if ok else "Check TWILIO credentials + A2P status."}
    except Exception as e:
        return {"name": "Twilio SMS", "configured": True, "ok": False,
                "latency_ms": _ms(t0), "error": f"{type(e).__name__}: {e}"[:200],
                "hint": "Twilio API unreachable."}


def _probe_gmail() -> dict:
    ok = emailer.is_configured()
    return {"name": "Gmail (email send)", "configured": ok,
            "ok": None if ok else None,
            "latency_ms": None, "error": None,
            "hint": None if ok else "Complete Google OAuth for Gmail."}


def _probe_calendar() -> dict:
    ok = gcal.is_configured()
    return {"name": "Google Calendar", "configured": ok,
            "ok": None if ok else None,
            "latency_ms": None, "error": None,
            "hint": None if ok else "Complete Google OAuth for Calendar."}


def _probe_social() -> dict:
    ok = social.is_configured()
    return {"name": "Social (Zapier)", "configured": ok,
            "ok": None if ok else None,
            "latency_ms": None, "error": None,
            "hint": None if ok else "Set the ZAPIER_* webhook URLs."}


def _probe_elevenlabs() -> dict:
    ok = voice_eleven.is_configured()
    return {"name": "ElevenLabs Voice", "configured": ok,
            "ok": None if ok else None,
            "latency_ms": None, "error": None,
            "hint": None if ok else "Set ELEVEN_API_KEY (optional — Edge TTS is fallback)."}


def _probe_xai() -> dict:
    ok = media_gen.is_configured()
    return {"name": "xAI Grok (image/video)", "configured": ok,
            "ok": None if ok else None,
            "latency_ms": None, "error": None,
            "hint": None if ok else "Set XAI_API_KEY."}


def _probe_dropbox() -> dict:
    if not files_dropbox.is_configured():
        return {"name": "Dropbox", "configured": False, "ok": None,
                "latency_ms": None, "error": None,
                "hint": "Set DROPBOX_ACCESS_TOKEN, or set DROPBOX_APP_KEY/SECRET and visit /api/dropbox/connect."}
    t0 = time.perf_counter()
    ok, err = files_dropbox.probe_connection()
    return {"name": "Dropbox", "configured": True, "ok": ok,
            "latency_ms": _ms(t0), "error": err,
            "hint": None if ok else "Token may be expired — reconnect at /api/dropbox/connect."}


def _probe_gdrive() -> dict:
    if not files_gdrive.is_configured():
        return {"name": "Google Drive", "configured": False, "ok": None,
                "latency_ms": None, "error": None,
                "hint": "Visit /api/drive/connect to grant Drive.readonly (needs GOOGLE_CLIENT_ID/SECRET)."}
    t0 = time.perf_counter()
    ok, err = files_gdrive.probe_connection()
    return {"name": "Google Drive", "configured": True, "ok": ok,
            "latency_ms": _ms(t0), "error": err,
            "hint": None if ok else "Reconnect at /api/drive/connect."}


def run_all() -> dict:
    """Run every probe and return {summary, counts, services, ran_at}."""
    started = time.perf_counter()
    services = []
    for probe in PROBES:
        try:
            services.append(probe())
        except Exception as e:
            services.append({
                "name": probe.__name__.replace("_probe_", "").title(),
                "configured": False, "ok": False,
                "latency_ms": None,
                "error": f"probe crashed: {type(e).__name__}: {e}"[:200],
                "hint": "Diagnostic probe itself failed — check server logs.",
            })

    total = len(services)
    red = sum(1 for s in services if s.get("ok") is False)
    green = sum(1 for s in services if s.get("ok") is True)
    unconfigured = sum(1 for s in services if not s.get("configured"))
    yellow = total - red - green - unconfigured

    if red:
        summary = "degraded"
    elif green and unconfigured == 0:
        summary = "healthy"
    else:
        summary = "partial"

    return {
        "summary": summary,
        "counts": {
            "green": green, "yellow": yellow, "red": red,
            "unconfigured": unconfigured, "total": total,
        },
        "services": services,
        "elapsed_ms": _ms(started),
    }
