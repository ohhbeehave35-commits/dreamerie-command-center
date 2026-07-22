"""
Social post queue + Zapier publishing bridge for the Command Center.

The flow mirrors outbound email's draft-then-confirm safety pattern:
  1. draft_social_post  -> saves a Draft row in the Airtable "Social Posts"
                           table (platform-tailored content). NEVER publishes.
  2. Owner reviews the draft in chat and explicitly approves.
  3. publish_social_post -> POSTs the content to the owner's Zapier webhook,
                           which fans out to the connected platform (Facebook,
                           YouTube, Instagram, TikTok, ...). Zapier holds the
                           platform logins, so no per-platform API approvals
                           are needed here.

The webhook URL is self-serve config (Airtable Settings key, editable from the
dashboard Settings panel) with an env-var fallback -- same pattern as Gmail.
If it's not set, publishing is simply "not connected" and says so honestly.
"""

from datetime import datetime, timezone

import os

import httpx

from . import crm

PLATFORMS = ["Facebook", "Instagram", "YouTube", "TikTok", "X"]
POSTS_TABLE = "Social Posts"
WEBHOOK_KEY = "zapier_webhook_url"

# Platforms that cannot post without media. Instagram requires a photo;
# YouTube and TikTok ARE video -- there is no text-only post on any of them.
# The media itself always comes from the owner (a URL to a photo/video asset;
# a future asset-library folder will feed this) -- the agent writes the words.
MEDIA_REQUIRED = {"Instagram": "a photo", "YouTube": "a video", "TikTok": "a video"}

_posts_table_id_cache = None


def _generic_webhook() -> str:
    """The original single-Zap webhook. Grandfathered to Facebook ONLY -- it
    was Facebook's Zap before per-platform routing existed. It must NOT be a
    fallback for other platforms, or an Instagram/TikTok post would silently
    go to Facebook's Zap."""
    return crm.get_setting(WEBHOOK_KEY, "") or os.environ.get("ZAPIER_WEBHOOK_URL", "")


def get_webhook_url(platform: str = "") -> str:
    """Webhook for a platform: its own key wins; Facebook alone falls back to
    the grandfathered generic key. Other platforms have no fallback -- if
    their key isn't set, they're simply not connected."""
    p = (platform or "").strip().lower()
    specific = crm.get_setting(f"{WEBHOOK_KEY}_{p}", "") if p else ""
    if specific:
        return specific
    if p in ("", "facebook"):
        return _generic_webhook()
    return ""


def is_configured() -> bool:
    """Can we publish to at least one platform? Drafting works regardless."""
    return any(connected_platforms().values())


def connected_platforms() -> dict:
    """Which platforms have a webhook actually wired. Facebook counts the
    grandfathered generic webhook; every other platform needs its own."""
    generic = bool(_generic_webhook())
    out = {}
    for p in PLATFORMS:
        specific = bool(crm.get_setting(f"{WEBHOOK_KEY}_{p.lower()}", ""))
        out[p] = specific or (generic if p == "Facebook" else False)
    return out


def _ensure_posts_table() -> str:
    """Return the Social Posts table id, creating it if needed."""
    global _posts_table_id_cache
    if _posts_table_id_cache:
        return _posts_table_id_cache
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{crm._API}/v0/meta/bases/{crm.AIRTABLE_BASE_ID}/tables", headers=crm._headers())
        r.raise_for_status()
        for t in r.json().get("tables", []):
            if t.get("name", "").lower() == POSTS_TABLE.lower():
                _posts_table_id_cache = t["id"]
                # The table may predate the Media URL column (it shipped after
                # the first Facebook-only version) -- add it to the live table
                # if missing, since writing an unknown field would error.
                existing = {f.get("name") for f in t.get("fields", [])}
                if "Media URL" not in existing:
                    c.post(f"{crm._API}/v0/meta/bases/{crm.AIRTABLE_BASE_ID}/tables/{t['id']}/fields",
                           headers=crm._headers(),
                           json={"name": "Media URL", "type": "singleLineText"})
                return _posts_table_id_cache
        # Primary (first) field must be a plain text type in Airtable.
        fields = [
            {"name": "Title", "type": "singleLineText"},
            {"name": "Platform", "type": "singleSelect", "options": {"choices": [
                {"name": p} for p in PLATFORMS]}},
            {"name": "Content", "type": "multilineText"},
            {"name": "Hashtags", "type": "singleLineText"},
            {"name": "Media URL", "type": "singleLineText"},
            {"name": "Status", "type": "singleSelect", "options": {"choices": [
                {"name": "Draft"}, {"name": "Published"}, {"name": "Failed"}]}},
            {"name": "Result", "type": "multilineText"},
        ]
        r = c.post(f"{crm._API}/v0/meta/bases/{crm.AIRTABLE_BASE_ID}/tables",
                   headers=crm._headers(), json={"name": POSTS_TABLE, "fields": fields})
        r.raise_for_status()
        _posts_table_id_cache = r.json()["id"]
        return _posts_table_id_cache


def create_draft(platform: str, content: str, title: str = "", hashtags: str = "",
                 media_url: str = "") -> str:
    """Save a Draft post. Returns a preview string (with the post id) or an
    explanation of why it couldn't save -- never raises."""
    if not crm.is_configured():
        return "The post queue isn't available (Airtable not connected), so I can't save drafts."
    platform = (platform or "").strip().title()
    if platform == "Youtube":
        platform = "YouTube"
    if platform.upper() == "X" or platform.lower() in ("twitter", "x/twitter"):
        platform = "X"
    if platform not in PLATFORMS:
        return f"Unknown platform {platform!r}. Supported: {', '.join(PLATFORMS)}."
    if not content.strip():
        return "The post needs some content before I can draft it."
    # Media-first platforms can't post words alone -- refuse honestly at draft
    # time rather than failing later at publish time.
    if platform in MEDIA_REQUIRED and not media_url.strip():
        return (
            f"{platform} posts require {MEDIA_REQUIRED[platform]} -- there's no "
            f"text-only post there. Ask the owner for a link to the "
            f"{MEDIA_REQUIRED[platform].split()[-1]} (or an asset from the media "
            f"folder), then draft again with it attached."
        )
    try:
        tid = _ensure_posts_table()
        with httpx.Client(timeout=30) as c:
            r = c.post(f"{crm._API}/v0/{crm.AIRTABLE_BASE_ID}/{tid}", headers=crm._headers(),
                       json={"fields": {
                           "Title": title[:200] or f"{platform} post",
                           "Platform": platform,
                           "Content": content[:100000],
                           "Hashtags": hashtags[:500],
                           "Media URL": media_url.strip()[:1000],
                           "Status": "Draft",
                       }, "typecast": True})
            r.raise_for_status()
            rec_id = r.json()["id"]
        media_line = f"\nMedia: {media_url.strip()}" if media_url.strip() else ""
        return (
            f"DRAFT saved (id {rec_id}) -- {platform}\n"
            f"Title: {title or '(none)'}{media_line}\n\n{content}\n\n{hashtags}".strip()
        )
    except Exception as e:
        return f"Couldn't save that draft: {type(e).__name__}: {e}"


def list_posts(status: str = "", limit: int = 10) -> str:
    """List recent posts in the queue, optionally filtered by Status."""
    if not crm.is_configured():
        return "The post queue isn't available (Airtable not connected)."
    try:
        tid = _ensure_posts_table()
        params = {"pageSize": str(max(1, min(limit, 25))),
                  "sort[0][field]": "Title", "sort[0][direction]": "desc"}
        if status:
            params["filterByFormula"] = "{Status}='" + status.replace("'", "") + "'"
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{crm._API}/v0/{crm.AIRTABLE_BASE_ID}/{tid}", headers=crm._headers(), params=params)
            r.raise_for_status()
        recs = r.json().get("records", [])
        if not recs:
            return "No posts in the queue" + (f" with status {status}" if status else "") + "."
        lines = []
        for rec in recs:
            f = rec.get("fields", {})
            lines.append(f"[{rec['id']}] {f.get('Status','?')} | {f.get('Platform','?')} | "
                         f"{f.get('Title','')} -- {f.get('Content','')[:80]}")
        return "\n".join(lines)
    except Exception as e:
        return f"Couldn't read the post queue: {type(e).__name__}: {e}"


def publish_post(post_id: str) -> str:
    """Send one Draft post to its platform's Zapier webhook and mark it
    Published/Failed. Returns a short confirmation or explanation -- never
    raises. The webhook is resolved per-platform AFTER reading the record
    (each platform can have its own Zap; the generic webhook is the fallback)."""
    if not crm.is_configured():
        return "The post queue isn't available (Airtable not connected)."
    try:
        tid = _ensure_posts_table()
        with httpx.Client(timeout=30) as c:
            r = c.get(f"{crm._API}/v0/{crm.AIRTABLE_BASE_ID}/{tid}/{post_id}", headers=crm._headers())
            if r.status_code == 404:
                return f"No post with id {post_id} in the queue."
            r.raise_for_status()
            fields = r.json().get("fields", {})
            if fields.get("Status") == "Published":
                return "That post was already published -- not sending it again."
            platform = fields.get("Platform", "")
            webhook = get_webhook_url(platform)
            if not webhook:
                return (f"Publishing to {platform or 'that platform'} isn't connected "
                        f"yet -- add its Zapier webhook URL in the Settings panel first.")
            payload = {
                "platform": platform,
                "title": fields.get("Title", ""),
                "content": fields.get("Content", ""),
                "hashtags": fields.get("Hashtags", ""),
                "media_url": fields.get("Media URL", ""),
                "post_id": post_id,
                "sent_at": datetime.now(timezone.utc).isoformat(),
            }
            hook = c.post(webhook, json=payload, timeout=30)
            ok = hook.status_code < 300
            c.patch(f"{crm._API}/v0/{crm.AIRTABLE_BASE_ID}/{tid}/{post_id}", headers=crm._headers(),
                    json={"fields": {
                        "Status": "Published" if ok else "Failed",
                        "Result": f"Webhook HTTP {hook.status_code}: {hook.text[:500]}",
                    }, "typecast": True})
            if ok:
                return f"Sent to Zapier for {fields.get('Platform','?')} -- it should appear on the platform shortly."
            return f"Zapier rejected it (HTTP {hook.status_code}) -- marked Failed. Check the Zap is on."
    except Exception as e:
        return f"Couldn't publish that post: {type(e).__name__}: {e}"
