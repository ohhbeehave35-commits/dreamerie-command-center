"""
Configuration validation and feature gating.

Checks which integrations are configured and disables tools/features accordingly.
Tools that depend on unconfigured integrations are removed from the tool list,
so Annabelle can't accidentally crash trying to use them.
"""

import os
from typing import Dict, List, Set


def _check_stripe() -> tuple[bool, str]:
    """Check if Stripe is configured."""
    if os.environ.get("STRIPE_SECRET_KEY"):
        return True, "Configured"
    return False, "Missing: STRIPE_SECRET_KEY"


def _check_twilio() -> tuple[bool, str]:
    """Check if Twilio SMS is configured."""
    required = ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"]
    missing = [k for k in required if not os.environ.get(k)]
    if not missing:
        return True, "Configured"
    return False, f"Missing: {', '.join(missing)}"


def _check_hubspot() -> tuple[bool, str]:
    """Check if HubSpot is configured."""
    if os.environ.get("HUBSPOT_ACCESS_TOKEN"):
        return True, "Configured"
    return False, "Missing: HUBSPOT_ACCESS_TOKEN"


def _check_buildertrend() -> tuple[bool, str]:
    """Check if Buildertrend is configured."""
    required = ["BUILDERTREND_ACCESS_TOKEN", "BUILDERTREND_ACCOUNT_ID"]
    missing = [k for k in required if not os.environ.get(k)]
    if not missing:
        return True, "Configured"
    return False, f"Missing: {', '.join(missing)}"


def _check_docusign() -> tuple[bool, str]:
    """Check if DocuSign is configured."""
    required = ["DOCUSIGN_ACCESS_TOKEN", "DOCUSIGN_ACCOUNT_ID"]
    missing = [k for k in required if not os.environ.get(k)]
    if not missing:
        return True, "Configured"
    return False, f"Missing: {', '.join(missing)}"


def _check_lightspeed() -> tuple[bool, str]:
    """Check if Lightspeed is configured."""
    required = ["LIGHTSPEED_ACCESS_TOKEN", "LIGHTSPEED_BUSINESS_ID"]
    missing = [k for k in required if not os.environ.get(k)]
    if not missing:
        return True, "Configured"
    return False, f"Missing: {', '.join(missing)}"


def _check_calendar() -> tuple[bool, str]:
    """Check if Google Calendar OAuth is configured."""
    # Import here to avoid circular dependency
    from . import calendar as gcal
    if gcal.is_configured():
        return True, "Configured"
    return False, "Needs: OAuth reconnection in Settings"


def _check_zapier() -> tuple[bool, str]:
    """Check if Zapier is configured."""
    if os.environ.get("ZAPIER_API_KEY"):
        return True, "Configured"
    return False, "Decision pending (deadline ~Aug 4)"


# Map integration names to their check functions
INTEGRATION_CHECKS = {
    "stripe": _check_stripe,
    "twilio": _check_twilio,
    "hubspot": _check_hubspot,
    "buildertrend": _check_buildertrend,
    "docusign": _check_docusign,
    "lightspeed": _check_lightspeed,
    "calendar": _check_calendar,
    "zapier": _check_zapier,
}

# Map tool names to the integrations they require
TOOL_DEPENDENCIES = {
    # Stripe
    "create_stripe_payment_link": {"stripe"},
    "create_stripe_invoice": {"stripe"},
    "create_subscription": {"stripe"},
    "update_subscription_price": {"stripe"},

    # Twilio SMS
    "send_sms": {"twilio"},

    # HubSpot
    "push_lead_to_hubspot": {"hubspot"},
    "search_hubspot_contact": {"hubspot"},
    "update_hubspot_contact": {"hubspot"},
    "update_hubspot_deal_stage": {"hubspot"},
    "get_hubspot_deals": {"hubspot"},

    # Buildertrend
    "get_buildertrend_jobs": {"buildertrend"},
    "send_buildertrend_message": {"buildertrend"},
    "update_buildertrend_milestone": {"buildertrend"},

    # DocuSign
    "send_proposal_docusign": {"docusign"},
    "get_docusign_status": {"docusign"},
    "download_signed_proposal": {"docusign"},

    # Lightspeed
    "create_lightspeed_invoice": {"lightspeed"},
    "get_lightspeed_inventory": {"lightspeed"},

    # Google Calendar
    "check_calendar_availability": {"calendar"},
    "book_calendar_event": {"calendar"},

    # Zapier
    "publish_social_post": {"zapier"},
    "send_social_draft_to_zapier": {"zapier"},
}


def get_configured_integrations() -> Dict[str, tuple[bool, str]]:
    """
    Check all integrations and return their status.

    Returns:
        Dict mapping integration name to (is_configured, status_message)
    """
    status = {}
    for name, check_fn in INTEGRATION_CHECKS.items():
        status[name] = check_fn()
    return status


def get_enabled_tool_names() -> Set[str]:
    """
    Get the set of tool names that are safe to pass to Claude.

    Removes any tool whose dependencies aren't all configured.

    Returns:
        Set of tool names that are enabled (all dependencies met)
    """
    configured = get_configured_integrations()
    configured_set = {name for name, (is_config, _) in configured.items() if is_config}

    enabled = set()
    for tool_name, dependencies in TOOL_DEPENDENCIES.items():
        if dependencies <= configured_set:
            # All dependencies are configured
            enabled.add(tool_name)

    return enabled


def filter_tools(all_tools: List[dict]) -> List[dict]:
    """
    Filter a tool list to only include tools whose dependencies are configured.

    Args:
        all_tools: Full list of tool definitions from agents.py

    Returns:
        Filtered list with unconfigured tools removed.
        Tools not in TOOL_DEPENDENCIES (i.e., those with no external dependencies)
        are always included.
    """
    enabled_names = get_enabled_tool_names()
    result = []
    for t in all_tools:
        tool_name = t.get("name")
        # Keep tool if: (1) it has no dependencies, OR (2) its dependencies are met
        if tool_name not in TOOL_DEPENDENCIES or tool_name in enabled_names:
            result.append(t)
    return result


def get_disabled_tools_report() -> Dict[str, List[str]]:
    """
    Generate a report of which tools are disabled and why.

    Returns:
        Dict mapping reason to list of tool names
    """
    configured = get_configured_integrations()
    configured_set = {name for name, (is_config, _) in configured.items() if is_config}

    disabled_report = {}
    for tool_name, dependencies in TOOL_DEPENDENCIES.items():
        missing = dependencies - configured_set
        if missing:
            for missing_integration in missing:
                _, status_msg = configured[missing_integration]
                reason = f"{missing_integration.upper()}: {status_msg}"
                if reason not in disabled_report:
                    disabled_report[reason] = []
                disabled_report[reason].append(tool_name)

    return disabled_report
