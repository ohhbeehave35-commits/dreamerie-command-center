"""
Autonomous health-check agent for production monitoring.

Runs on a schedule (e.g., GitHub Actions cron every 6 hours).
Checks /api/health endpoint and reports anomalies.
"""

import os
import httpx
from datetime import datetime, timezone


def run_health_check(base_url: str) -> dict:
    """
    Check the health of a running Stinger Command Center instance.

    Returns:
        {
            "timestamp": ISO timestamp,
            "status": "healthy" | "degraded" | "critical",
            "integrations": {name: {status, message}},
            "issues": [{severity, category, detail}],
            "recommendations": [str]
        }
    """
    issues = []
    recommendations = []
    status = "healthy"

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(f"{base_url}/api/health")
            resp.raise_for_status()
            health_data = resp.json()
    except Exception as e:
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "critical",
            "integrations": {},
            "issues": [{"severity": "critical", "category": "connectivity", "detail": f"Could not reach /api/health: {e}"}],
            "recommendations": [f"Check if {base_url} is reachable and healthy."]
        }

    # Parse integrations
    integrations = health_data.get("integrations", {})
    for name, info in integrations.items():
        is_configured = info.get("status") == "Configured"

        if not is_configured:
            # Unconfigured is expected for optional integrations; only warn if it's required
            required = name in ["Anthropic", "Airtable", "Stripe"]
            if required:
                issues.append({
                    "severity": "warning",
                    "category": name,
                    "detail": f"{name} is not configured."
                })
                recommendations.append(f"Configure {name} in Render env vars or Settings.")
                status = "degraded"

    # Check for stale timestamps (older than 30 days likely means a token expired)
    for name in ["Google Calendar", "Dropbox"]:
        if name in integrations:
            last_check = integrations[name].get("last_check", "")
            if last_check:
                try:
                    check_time = datetime.fromisoformat(last_check.replace("Z", "+00:00"))
                    days_ago = (datetime.now(timezone.utc) - check_time).days
                    if days_ago > 30:
                        issues.append({
                            "severity": "warning",
                            "category": name,
                            "detail": f"{name} last checked {days_ago} days ago (token likely stale)."
                        })
                        recommendations.append(f"Reconnect {name} in Settings → {name}.")
                        status = "degraded"
                except:
                    pass

    # Determine overall status
    if any(i["severity"] == "critical" for i in issues):
        status = "critical"
    elif any(i["severity"] == "warning" for i in issues):
        status = "degraded"
    else:
        status = "healthy"

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "integrations": integrations,
        "issues": issues,
        "recommendations": recommendations
    }


if __name__ == "__main__":
    import sys
    base_url = sys.argv[1] if len(sys.argv) > 1 else "https://dreamerie-command-center.onrender.com"
    result = run_health_check(base_url)
    print(result)
