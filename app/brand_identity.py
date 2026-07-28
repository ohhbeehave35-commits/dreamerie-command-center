"""Per-brand sending identity, so one deployment can act as several businesses
without either one's mail or calendar leaking into the other.

Every outbound credential in this app used to be a single global key -- one
`gmail_address`, one `gmail_app_password`, one `google_calendar_id` for the
whole deployment. Every mode's tool set exposes `send_email`, and all of them
resolved that same global key, so Annabelle answering as Bear Arms sent from
The Dreamerie's address and wrote to its calendar. There was no business dimension anywhere. This module is it.

Two setups are supported by the SAME keys, so you can start on one and move to
the other later without a code change:

  Aliases -- one Google login, one verified "send mail as" address per brand.
      Set gmail_address__<brand>. Leave gmail_app_password__<brand> unset:
      every brand logs in as the shared account and only the From differs.

  Separate accounts -- one Google login per brand.
      Set BOTH gmail_address__<brand> and gmail_app_password__<brand>.

Switching a single brand from the first to the second is just filling in that
brand's password. Nothing here changes.

DELIBERATELY NO FALLBACK to the global address. A brand with nothing set up
refuses to send and says which key is missing. Falling back to the main account
is precisely the bleed this exists to stop, and a wrong-brand email to a client
cannot be unsent. Refusing is recoverable; sending is not.
"""

import os

from . import crm

# Brand key -> human label. Keys MUST match the chat `mode` values in main.py
# ("dreamerie" | "suzy_d" | "bear_arms" | "peptides"), because the active
# mode is what selects the
# identity. "combined" is intentionally absent: it is not a brand, and a send
# from it has no single correct From address.
BRANDS = {
    "dreamerie": "The Dreamerie",
    "suzy_d": "Suzy D",
    "bear_arms": "Bear Arms",
    "peptides": "NS Peptides",
}

GMAIL_ADDRESS_BASE = "gmail_address"
GMAIL_APP_PASSWORD_BASE = "gmail_app_password"
CALENDAR_ID_BASE = "google_calendar_id"


def scoped_key(base: str, brand: str) -> str:
    """The per-brand settings key. Double underscore so it can never collide
    with a real global key name."""
    return f"{base}__{brand}"


def _setting(base: str, brand: str) -> str:
    """Per-brand value from Airtable, else the matching per-brand env var.

    Mirrors the precedence the global keys already use (Settings panel beats
    env), so a brand can be connected live without a Render redeploy.
    """
    val = crm.get_setting(scoped_key(base, brand), "")
    if val:
        return val
    return os.environ.get(scoped_key(base, brand).upper(), "")


def is_brand(brand: str) -> bool:
    return brand in BRANDS


def label(brand: str) -> str:
    return BRANDS.get(brand, brand)


def brand_list() -> str:
    """Human-readable brand names, for error messages the model reads aloud."""
    return " or ".join(BRANDS.values())


def resolve_email(brand: str) -> dict:
    """Work out which identity a send should use.

    Returns {"ok": True, "from": ..., "login": ..., "password": ..., "alias": bool}
    or {"ok": False, "error": "<plain-English reason>"}. Never raises, and never
    silently substitutes a different brand's identity.
    """
    if not brand or brand == "combined":
        return {
            "ok": False,
            "error": (
                "I'm in Combined mode, so I don't know which business this should come "
                f"from. Tell me {brand_list()} and I'll send it from that one."
            ),
        }
    if not is_brand(brand):
        return {"ok": False, "error": f"I don't have a business set up called {brand!r}."}

    from_address = _setting(GMAIL_ADDRESS_BASE, brand)
    if not from_address:
        return {
            "ok": False,
            "error": (
                f"{label(brand)} doesn't have its own sending address set up yet, so I "
                f"won't send this -- it would go out under a different business. Add "
                f"'{scoped_key(GMAIL_ADDRESS_BASE, brand)}' in Settings first."
            ),
        }

    # Per-brand password = its own Google account. No per-brand password = alias
    # mode: log in as the shared account, send as the brand's verified address.
    brand_password = _setting(GMAIL_APP_PASSWORD_BASE, brand)
    if brand_password:
        return {"ok": True, "from": from_address, "login": from_address,
                "password": brand_password, "alias": False}

    shared_login = crm.get_setting(GMAIL_APP_PASSWORD_BASE, "") or os.environ.get("GMAIL_APP_PASSWORD", "")
    shared_address = crm.get_setting(GMAIL_ADDRESS_BASE, "") or os.environ.get("GMAIL_ADDRESS", "")
    if not (shared_login and shared_address):
        return {
            "ok": False,
            "error": (
                f"{label(brand)} is set to send as {from_address}, but there's no Google "
                f"login behind it yet. Either add '{scoped_key(GMAIL_APP_PASSWORD_BASE, brand)}' "
                f"for its own account, or connect the main email account in Settings."
            ),
        }
    return {"ok": True, "from": from_address, "login": shared_address,
            "password": shared_login, "alias": True}


def resolve_calendar_id(brand: str) -> str:
    """The brand's calendar, or "" if it has none.

    Callers must treat "" as "not set up" rather than falling back to
    "primary" -- writing one brand's events onto another's calendar is the same
    bleed as sending from the wrong address, just quieter.
    """
    if not is_brand(brand):
        return ""
    return _setting(CALENDAR_ID_BASE, brand)


def status() -> dict:
    """Which brands are actually wired, for the diagnostic board and Settings.

    Reports whether each brand can send and how, WITHOUT ever returning a
    password (only whether one exists).
    """
    out = {}
    for brand, name in BRANDS.items():
        r = resolve_email(brand)
        out[brand] = {
            "label": name,
            "can_send": bool(r.get("ok")),
            "from": r.get("from", ""),
            "mode": ("own account" if r.get("ok") and not r.get("alias")
                     else "alias on shared login" if r.get("ok") else "not set up"),
            "calendar": resolve_calendar_id(brand) or "(none)",
            "why_not": "" if r.get("ok") else r.get("error", ""),
        }
    return out
