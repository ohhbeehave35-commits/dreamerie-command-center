"""
Stripe billing integration for Stinger Industries.

Handles online payment links, invoices, and subscriptions for client billing.
Works alongside Lightspeed (lightspeed.py handles internal invoicing/records;
Stripe handles actual online payment collection from clients).

Env vars required (set in Render):
    STRIPE_SECRET_KEY       -- sk_live_... or sk_test_... from stripe.com/apikeys
    STRIPE_WEBHOOK_SECRET   -- whsec_... from stripe.com/webhooks (for event verification)

Usage:
    from . import stripe_billing as stripe
    link = stripe.create_payment_link("Jane Doe", "jane@example.com",
                                      [{"name": "Command Center Build", "amount": 2500_00}])
"""

import os
import json
import hmac
import hashlib
import time
import httpx
import logging
from typing import Any

log = logging.getLogger(__name__)

_BASE = "https://api.stripe.com/v1"

MIN_AMOUNT_CENTS = 100         # $1.00 — Stripe's own minimum
MAX_AMOUNT_CENTS = 50_000_00   # $500,000 hard cap — anything above requires manual billing



def _validate_amount(amount_cents: int) -> int:
    if not isinstance(amount_cents, int):
        raise ValueError(f"amount_cents must be int, got {type(amount_cents).__name__}")
    if amount_cents < MIN_AMOUNT_CENTS:
        raise ValueError(f"Amount {amount_cents}¢ is below the $1.00 minimum")
    if amount_cents > MAX_AMOUNT_CENTS:
        raise ValueError(f"Amount ${amount_cents / 100:,.2f} exceeds the $500,000 cap — bill manually")
    return amount_cents


def _headers() -> dict:
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not key:
        raise ValueError("STRIPE_SECRET_KEY not set in Render environment")
    return {"Authorization": f"Bearer {key}"}


def is_configured() -> bool:
    return bool(os.environ.get("STRIPE_SECRET_KEY"))


def _post(path: str, data: dict) -> Any:
    resp = httpx.post(
        f"{_BASE}/{path}",
        headers=_headers(),
        data=data,   # Stripe uses form-encoded, not JSON
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _get(path: str, params: dict | None = None) -> Any:
    resp = httpx.get(
        f"{_BASE}/{path}",
        headers=_headers(),
        params=params or {},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


# ── CUSTOMERS ─────────────────────────────────────────────────────────────────

def create_or_find_customer(name: str, email: str, phone: str = "") -> dict:
    """
    Find a Stripe customer by email or create one if not found.
    Returns the Stripe customer object.
    """
    # Search first
    if email:
        result = _get("customers", {"email": email, "limit": 1})
        if result.get("data"):
            log.info("Found existing Stripe customer for %s", email)
            return result["data"][0]

    # Create new
    data: dict = {"name": name, "email": email}
    if phone:
        data["phone"] = phone
    return _post("customers", data)


# ── PRODUCTS & PRICES ─────────────────────────────────────────────────────────

def create_price(name: str, amount_cents: int, recurring: bool = False,
                 interval: str = "month") -> dict:
    """
    Create a Stripe Price (one-time or recurring) for a named product.
    `amount_cents`: amount in cents (e.g. 250000 for the $2,500 standard build).
    Returns the Price object.
    """
    amount_cents = _validate_amount(amount_cents)
    # Create the product first
    product = _post("products", {"name": name})
    product_id = product["id"]

    price_data: dict = {
        "currency": "usd",
        "unit_amount": str(amount_cents),
        "product": product_id,
    }
    if recurring:
        price_data["recurring[interval]"] = interval

    return _post("prices", price_data)


# ── PAYMENT LINKS ─────────────────────────────────────────────────────────────

def create_payment_link(
    customer_name: str,
    customer_email: str,
    line_items: list[dict],
    customer_phone: str = "",
    after_completion_url: str = "",
) -> dict:
    """
    Create a Stripe Payment Link the client can click to pay online.

    line_items: list of dicts:
        [{"name": "Command Center Build", "amount": 250000, "qty": 1}]
        amount is in CENTS (multiply dollars by 100).

    Returns {"url": "https://buy.stripe.com/...", "payment_link_id": "plink_..."}
    """
    # Create prices for each line item
    price_ids = []
    for item in line_items:
        price = create_price(
            name=item["name"],
            amount_cents=item["amount"],
        )
        price_ids.append((price["id"], item.get("qty", 1)))

    # Build the line_items form data for Stripe (indexed)
    link_data: dict = {}
    for i, (price_id, qty) in enumerate(price_ids):
        link_data[f"line_items[{i}][price]"] = price_id
        link_data[f"line_items[{i}][quantity]"] = str(qty)

    # Prefill customer email if provided
    if customer_email:
        link_data["customer_email"] = customer_email

    if after_completion_url:
        link_data["after_completion[type]"] = "redirect"
        link_data["after_completion[redirect][url]"] = after_completion_url

    result = _post("payment_links", link_data)
    log.info("Created Stripe payment link %s for %s", result["id"], customer_name)
    return {"url": result["url"], "payment_link_id": result["id"], "raw": result}


# ── INVOICES ──────────────────────────────────────────────────────────────────

def create_invoice(
    customer_name: str,
    customer_email: str,
    line_items: list[dict],
    customer_phone: str = "",
    due_days: int = 7,
    memo: str = "",
    auto_send: bool = True,
) -> dict:
    """
    Create and finalize a Stripe Invoice, optionally sending it to the client.

    line_items: [{"name": "...", "amount": <cents>, "qty": 1}]

    Returns the finalized Invoice object (includes hosted_invoice_url for the client).
    """
    customer = create_or_find_customer(customer_name, customer_email, customer_phone)
    customer_id = customer["id"]

    # Add invoice items before creating the invoice
    for item in line_items:
        price = create_price(name=item["name"], amount_cents=item["amount"])
        _post("invoiceitems", {
            "customer": customer_id,
            "price": price["id"],
            "quantity": str(item.get("qty", 1)),
        })

    # Create invoice
    invoice_data: dict = {
        "customer": customer_id,
        "collection_method": "send_invoice",
        "days_until_due": str(due_days),
    }
    if memo:
        invoice_data["description"] = memo

    invoice = _post("invoices", invoice_data)
    invoice_id = invoice["id"]

    # Finalize (locks the invoice)
    invoice = _post(f"invoices/{invoice_id}/finalize", {})

    # Send it to the client if requested
    if auto_send:
        invoice = _post(f"invoices/{invoice_id}/send", {})
        log.info("Stripe invoice %s sent to %s", invoice_id, customer_email)

    return invoice


def list_invoices(customer_email: str = "", limit: int = 20) -> list[dict]:
    """List recent Stripe invoices, optionally filtered by customer email."""
    params: dict = {"limit": limit}
    if customer_email:
        customers = _get("customers", {"email": customer_email, "limit": 1})
        if customers.get("data"):
            params["customer"] = customers["data"][0]["id"]
    result = _get("invoices", params)
    return result.get("data", [])


# ── SUBSCRIPTIONS (retainer billing) ──────────────────────────────────────────

def create_subscription(
    customer_name: str,
    customer_email: str,
    plan_name: str,
    amount_cents: int,
    interval: str = "month",
    trial_days: int = 0,
) -> dict:
    """
    Create a monthly (or other interval) subscription for retainer billing.
    Typical use: $497/mo months 1-6, then upgrade to $697/mo at month 7.

    Returns the Stripe Subscription object.
    """
    customer = create_or_find_customer(customer_name, customer_email)
    customer_id = customer["id"]

    price = create_price(plan_name, amount_cents, recurring=True, interval=interval)

    sub_data: dict = {
        "customer": customer_id,
        "items[0][price]": price["id"],
    }
    if trial_days:
        sub_data["trial_period_days"] = str(trial_days)

    subscription = _post("subscriptions", sub_data)
    log.info("Stripe subscription %s created for %s", subscription["id"], customer_email)
    return subscription


def update_subscription_price(subscription_id: str, new_amount_cents: int,
                               plan_name: str) -> dict:
    """
    Update a subscription to a new price (e.g., month-7 retainer bump $497→$697).
    Creates a new Price and replaces the current subscription item.
    """
    # Get current subscription
    sub = _get(f"subscriptions/{subscription_id}")
    item_id = sub["items"]["data"][0]["id"]

    # Create the new price
    new_price = create_price(plan_name, new_amount_cents, recurring=True)

    # Update
    updated = _post(f"subscriptions/{subscription_id}", {
        "items[0][id]": item_id,
        "items[0][price]": new_price["id"],
        "proration_behavior": "always_invoice",
    })
    log.info("Subscription %s updated to %d cents/mo", subscription_id, new_amount_cents)
    return updated


# ── WEBHOOK VERIFICATION ──────────────────────────────────────────────────────

def verify_webhook(payload_bytes: bytes, stripe_signature: str) -> dict | None:
    """
    Verify a Stripe webhook signature and return the event dict, or None if invalid.
    Call this in the FastAPI webhook route before trusting any payload.
    """
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    if not secret:
        log.error("STRIPE_WEBHOOK_SECRET not set — rejecting webhook (fail-closed)")
        return None

    try:
        # Stripe signature format: t=<timestamp>,v1=<sig>[,v1=<sig2>...]
        # During secret rotation Stripe sends multiple v1 signatures; collect all.
        timestamp = ""
        sigs = []
        for part in stripe_signature.split(","):
            k, _, v = part.partition("=")
            if k == "t":
                timestamp = v
            elif k == "v1":
                sigs.append(v)

        if not timestamp or not sigs:
            log.warning("Stripe webhook missing timestamp or signature")
            return None

        # Replay attack guard: reject if timestamp is >5 minutes old
        if abs(time.time() - int(timestamp)) > 300:
            log.warning("Stripe webhook timestamp too old — possible replay attack")
            return None

        signed_payload = f"{timestamp}.{payload_bytes.decode()}"
        expected = hmac.new(secret.encode(), signed_payload.encode(), hashlib.sha256).hexdigest()
        if not any(hmac.compare_digest(expected, s) for s in sigs):
            log.warning("Stripe webhook signature mismatch")
            return None

        return json.loads(payload_bytes)
    except Exception as e:
        log.error("Stripe webhook verification error: %s", e)
        return None


# ── CONVENIENCE ───────────────────────────────────────────────────────────────

def bill_new_client(
    name: str,
    email: str,
    phone: str = "",
    build_fee_cents: int = 250000,
    retainer_cents: int = 29700,
    annual_prepay: bool = False,
) -> dict:
    """
    Full onboarding billing flow for a new Stinger client:
    1. Creates a payment link for the build fee (client pays online)
    2. Creates a retainer subscription -- monthly by default; yearly at 11x the
       monthly rate if annual_prepay is True (12-for-11, approved 27 Jul 2026)
    Returns both.

    Defaults are the STANDARD ladder (locked 27 Jul 2026), not any client's deal:
      build   $2,995 less the $495 launch credit = $2,500, paid in full at signing
      monthly $297 Launch  ($497 Ignite / $697 Accelerate must be passed explicitly)

    These defaulted to $2,997 + $295 -- Louden's rate -- which meant a new client
    billed without explicit amounts got another client's private price. Founder and
    legacy rates are never defaults: pass them per-deal or read them off the signed
    agreement.

    Annual policy: never offered at signing (month 2-3 at the earliest), collected
    by ACH bank transfer, and unused full months are refunded at the standard
    monthly rate on cancellation -- annual_prepay=True here exists for the
    month-2-3 conversion, not the onboarding call.
    """
    link = create_payment_link(
        customer_name=name,
        customer_email=email,
        customer_phone=phone,
        line_items=[{"name": "Stinger Industries — AI Command Center Build", "amount": build_fee_cents}],
    )
    if annual_prepay:
        subscription = create_subscription(
            customer_name=name,
            customer_email=email,
            plan_name="Stinger Industries — Annual Hosting & Support (12 months for the price of 11)",
            amount_cents=retainer_cents * 11,
            interval="year",
        )
    else:
        subscription = create_subscription(
            customer_name=name,
            customer_email=email,
            plan_name="Stinger Industries — Monthly Hosting & Support (Months 1-6)",
            amount_cents=retainer_cents,
            trial_days=30,  # First retainer charge after first month
        )
    return {"payment_link": link, "subscription": subscription}
