"""
Web Push notifications -- lets Annabelle's alerts reach the owner as a real
OS-level notification on their phone, even when the Command Center PWA isn't
open in front of them.

Standard browser Push API + VAPID, no third-party service (no Firebase, no
per-message cost). Configure with two env vars:
    VAPID_PRIVATE_KEY  - base64url EC private key (generate once, keep secret)
    VAPID_PUBLIC_KEY   - base64url EC public key (safe to expose to the frontend)
    VAPID_SUBJECT       - optional, defaults to a mailto: contact for push services

If unset, push is simply "not connected" -- alerts still work as in-app
floating cards, this only adds the phone-lock-screen layer on top.
"""

import logging
import os

from . import crm

log = logging.getLogger(__name__)

VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:admin@dreamerie.com")


def is_configured() -> bool:
    return bool(VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY)


def send_to_owner(title: str, body: str, url: str = "/static/basic.html") -> int:
    """Push `title`/`body` to every subscribed owner device. Returns the
    number of devices successfully notified. Never raises -- a push failure
    should never break the chat reply it's riding along with."""
    if not is_configured():
        return 0
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        log.warning("push.send_to_owner: pywebpush not installed")
        return 0

    import json
    payload = json.dumps({"title": title[:120], "body": (body or "")[:180], "url": url})
    sent = 0
    for sub in crm.list_push_subscriptions():
        subscription_info = {
            "endpoint": sub["endpoint"],
            "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
        }
        try:
            webpush(
                subscription_info=subscription_info,
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_SUBJECT},
            )
            sent += 1
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):
                # Push service says this subscription is dead (uninstalled,
                # browser data cleared, etc.) -- stop trying it forever.
                crm.remove_push_subscription(sub["endpoint"])
            else:
                log.warning("push send failed (%s): %s", status, e)
        except Exception as e:
            log.warning("push send error: %s", e)
    return sent
