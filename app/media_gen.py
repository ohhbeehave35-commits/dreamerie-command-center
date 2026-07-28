"""
AI image + video generation via xAI's Grok Imagine API and OpenAI's DALL-E.

Lets Annabelle generate marketing visuals (graphics, animations, short clips)
on demand -- in the public widget or owner chat -- instead of requiring a
human to manually prompt Grok/ChatGPT and hand back a file (see Stinger
Playbook builder-skill 31 for the manual-workflow predecessor to this).
Real, metered cost per call (xAI bills per image; video is $0.06/sec) -- see
main.py's IMAGE_MONTHLY_CAP / VIDEO_MONTHLY_CAP circuit breakers, same
pattern as the existing web-search cap.

Images can route to either ChatGPT (DALL-E 3) or Grok (xAI), depending on
media_image_provider setting. Videos use Grok only.

Reuses the same XAI_API_KEY already configured for Grok TTS (same xAI
account/billing) -- no new secret required. Self-serve Airtable Settings
override ("xai_api_key") wins over the env var, same as every other
connector in this app. Same for OpenAI_API_KEY.

Images are synchronous (typically a few seconds). Video is xAI's async job
model: POST starts a request_id, then poll GET until status is "done" --
~25s for an 8s clip on the fast model. This function blocks for the whole
wait rather than a true async follow-up; acceptable given how rarely video
is likely to be requested -- revisit if it becomes a bottleneck.
"""

import os
import time

import httpx

from . import crm

API_BASE = "https://api.x.ai/v1"
IMAGE_MODEL = os.environ.get("XAI_IMAGE_MODEL", "grok-imagine-image-quality")
VIDEO_MODEL = os.environ.get("XAI_VIDEO_MODEL", "grok-imagine-video")
VIDEO_POLL_TIMEOUT_S = 90
VIDEO_POLL_INTERVAL_S = 5


def get_xai_api_key() -> str:
    return crm.get_setting("xai_api_key", "") or os.environ.get("XAI_API_KEY", "")


def get_openai_api_key() -> str:
    if crm.is_configured():
        key = crm.get_setting("openai_api_key", "")
        if key:
            return key
    return os.environ.get("OPENAI_API_KEY", "")


def get_api_key() -> str:
    """Legacy: returns xAI key for backward compatibility."""
    return get_xai_api_key()


def is_configured() -> bool:
    """Check if at least one provider is configured."""
    return bool(get_xai_api_key()) or bool(get_openai_api_key())


def is_grok_configured() -> bool:
    return bool(get_xai_api_key())


def is_chatgpt_configured() -> bool:
    return bool(get_openai_api_key())


def _xai_headers() -> dict:
    return {"Authorization": f"Bearer {get_xai_api_key()}", "Content-Type": "application/json"}


def _openai_headers() -> dict:
    return {"Authorization": f"Bearer {get_openai_api_key()}", "Content-Type": "application/json"}


def generate_image_grok(prompt: str, aspect_ratio: str = "") -> dict:
    """Generate one image via Grok (xAI).

    Returns {'ok': True, 'url': str} or {'ok': False, 'error': str}
    ('error' is 'not_connected' if no API key is set). Never raises.
    """
    if not is_grok_configured():
        return {"ok": False, "error": "grok_not_connected"}
    if not prompt.strip():
        return {"ok": False, "error": "empty prompt"}
    try:
        body = {"model": IMAGE_MODEL, "prompt": prompt.strip()[:4000], "n": 1}
        if aspect_ratio.strip():
            body["aspect_ratio"] = aspect_ratio.strip()
        with httpx.Client(timeout=60) as c:
            r = c.post(f"{API_BASE}/images/generations", headers=_xai_headers(), json=body)
        if r.status_code != 200:
            return {"ok": False, "error": f"{r.status_code}: {r.text[:300]}"}
        data = r.json().get("data", [])
        if not data or not data[0].get("url"):
            return {"ok": False, "error": "empty response from xAI"}
        return {"ok": True, "url": data[0]["url"]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def generate_image_chatgpt(prompt: str, size: str = "1024x1024", aspect_ratio: str = "") -> dict:
    """Generate one image via ChatGPT (DALL-E 3).

    Returns {'ok': True, 'url': str} or {'ok': False, 'error': str}
    ('error' is 'not_connected' if no API key is set). Never raises.
    """
    if not is_chatgpt_configured():
        return {"ok": False, "error": "chatgpt_not_connected"}
    if not prompt.strip():
        return {"ok": False, "error": "empty prompt"}
    try:
        # Map aspect_ratio to DALL-E 3 size format
        if aspect_ratio.strip():
            ratio_map = {
                "16:9": "1792x1024",
                "9:16": "1024x1792",
                "1:1": "1024x1024",
            }
            size = ratio_map.get(aspect_ratio.strip(), "1024x1024")

        body = {
            "model": "dall-e-3",
            "prompt": prompt.strip()[:4000],
            "n": 1,
            "size": size,
            "quality": "standard",
        }
        with httpx.Client(timeout=60) as c:
            r = c.post(
                "https://api.openai.com/v1/images/generations",
                headers=_openai_headers(),
                json=body
            )
        if r.status_code != 200:
            return {"ok": False, "error": f"{r.status_code}: {r.text[:300]}"}
        data = r.json().get("data", [])
        if not data or not data[0].get("url"):
            return {"ok": False, "error": "empty response from OpenAI"}
        return {"ok": True, "url": data[0]["url"]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def generate_image(prompt: str, aspect_ratio: str = "") -> dict:
    """Generate one image, routing to ChatGPT or Grok based on settings.

    Returns {'ok': True, 'url': str, 'provider': str} or {'ok': False, 'error': str}.
    Provider is 'chatgpt' or 'grok' — tracks which succeeded, not which was preferred.
    Never raises.
    """
    if not prompt.strip():
        return {"ok": False, "error": "empty prompt"}

    provider = crm.get_setting("media_image_provider", "chatgpt") if crm.is_configured() else "chatgpt"

    if provider == "chatgpt":
        result = generate_image_chatgpt(prompt, aspect_ratio=aspect_ratio)
        if result.get("ok"):
            return {**result, "provider": "chatgpt"}
        if result.get("error") == "chatgpt_not_connected" and is_grok_configured():
            result = generate_image_grok(prompt, aspect_ratio)
            if result.get("ok"):
                return {**result, "provider": "grok"}
            return result
        return result
    else:  # grok
        result = generate_image_grok(prompt, aspect_ratio)
        if result.get("ok"):
            return {**result, "provider": "grok"}
        if result.get("error") == "grok_not_connected" and is_chatgpt_configured():
            result = generate_image_chatgpt(prompt, aspect_ratio=aspect_ratio)
            if result.get("ok"):
                return {**result, "provider": "chatgpt"}
            return result
        return result


def generate_video(prompt: str, duration: int = 8, aspect_ratio: str = "", image_url: str = "") -> dict:
    """Generate one video from a text prompt via Grok (xAI). Blocks while polling
    xAI's async job until done or VIDEO_POLL_TIMEOUT_S elapses.

    Returns {'ok': True, 'url': str, 'duration': int} or
    {'ok': False, 'error': str} ('error' is 'not_connected' if no API key is
    set, or 'timeout' if still processing when we stopped waiting). Never
    raises.
    """
    if not is_grok_configured():
        return {"ok": False, "error": "not_connected"}
    if not prompt.strip():
        return {"ok": False, "error": "empty prompt"}
    try:
        duration = max(1, min(int(duration or 8), 15))
        body = {"model": VIDEO_MODEL, "prompt": prompt.strip()[:4000], "duration": duration}
        if aspect_ratio.strip():
            body["aspect_ratio"] = aspect_ratio.strip()
        if image_url.strip():
            body["image"] = image_url.strip()
        with httpx.Client(timeout=30) as c:
            r = c.post(f"{API_BASE}/videos/generations", headers=_xai_headers(), json=body)
        if r.status_code != 200:
            return {"ok": False, "error": f"{r.status_code}: {r.text[:300]}"}
        request_id = r.json().get("request_id", "")
        if not request_id:
            return {"ok": False, "error": "no request_id returned"}
        deadline = time.time() + VIDEO_POLL_TIMEOUT_S
        with httpx.Client(timeout=30) as c:
            while time.time() < deadline:
                time.sleep(VIDEO_POLL_INTERVAL_S)
                pr = c.get(f"{API_BASE}/videos/{request_id}", headers=_xai_headers())
                if pr.status_code != 200:
                    continue
                pj = pr.json()
                status = pj.get("status")
                if status == "done":
                    video = pj.get("video") or {}
                    if not video.get("url"):
                        return {"ok": False, "error": "done but no video URL returned"}
                    return {"ok": True, "url": video["url"], "duration": video.get("duration", duration)}
                if status == "failed":
                    return {"ok": False, "error": "generation failed on xAI's side"}
        return {"ok": False, "error": "timeout"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
