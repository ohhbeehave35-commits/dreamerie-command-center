"""
ElevenLabs text-to-speech for the Command Center.

Same self-serve credential pattern as Gmail/Calendar/Social: the API key is
an Airtable Settings override (paste-and-connect from the dashboard) with an
env-var bootstrap fallback. If it's not set, TTS simply falls through to the
next engine in main.py's chain (Grok, then free Edge) -- never crashes.

ElevenLabs is the TOP of that quality chain: paid, commercial-licensed,
professional voice cloning available on the account's Creator plan. Kept as
its own module (not folded into main.py) so the credential logic, voice list,
and API call are one place to look, matching the emailer.py / calendar.py /
social.py shape.
"""

import os

import httpx

from . import crm

API_KEY_SETTING = "elevenlabs_api_key"
VOICE_ID_SETTING = "elevenlabs_voice_id"

# Delivery tuning. All env-overridable so pace/energy can be dialled in from
# Render without a code change or redeploy of logic.
#
# model: eleven_flash_v2_5. ElevenLabs' own docs say the flash and turbo v2.5
#   models are functionally EQUIVALENT in output, flash just answers faster
#   (~75ms model latency vs ~250-300ms), and they recommend flash in all use
#   cases. Replies are spoken sentence-by-sentence, so this latency is paid on
#   EVERY sentence -- the single biggest dead-air lever. eleven_multilingual_v2
#   remains the right override for slow long-form narration.
# stability: LOWER = more expressive/variable. 0.5 reads flat and even -- the
#   "lethargic" quality. ~0.4 gives her more natural pitch movement.
# style: adds emphasis and energy. 0 (the old implicit default) is deadpan.
# speed: 1.0 is baseline; range 0.7-1.2. A slight push reads as engaged.
ELEVEN_MODEL = os.environ.get("ELEVENLABS_MODEL", "eleven_flash_v2_5")
ELEVEN_STABILITY = float(os.environ.get("ELEVENLABS_STABILITY", "0.40"))
ELEVEN_SIMILARITY = float(os.environ.get("ELEVENLABS_SIMILARITY", "0.75"))
ELEVEN_STYLE = float(os.environ.get("ELEVENLABS_STYLE", "0.35"))
ELEVEN_SPEED = float(os.environ.get("ELEVENLABS_SPEED", "1.08"))

# A handful of well-known, good-quality stock voices from ElevenLabs' public
# voice library -- a reasonable curated default list, same spirit as
# TTS_VOICE_OPTIONS for Edge. The owner can also paste a cloned voice's ID
# directly into Settings once cloning is set up.
DEFAULT_VOICES = {
    "Rachel (warm, narration)": "21m00Tcm4TlvDq8ikWAM",
    "Adam (deep, confident)": "pNInz6obpgDQGcFmaJgB",
    "Bella (soft, friendly)": "EXAVITQu4vr4xnSDxMaL",
    "Antoni (calm, well-rounded)": "ErXwobaYiN019PkySvjV",
    "Domi (strong, assertive)": "AZnzlk1XvdvUeBnXmlld",
}


# One shared connection pool for every clip. A fresh AsyncClient per sentence
# meant a fresh TLS handshake per sentence -- 100-300ms of pure dead air added
# to EVERY clip in a spoken reply. Keep-alive makes clip 2..n start warm.
_http = httpx.AsyncClient(timeout=30)


def get_api_key() -> str:
    return crm.get_setting(API_KEY_SETTING, "") or os.environ.get("ELEVENLABS_API_KEY", "")


def get_voice_id() -> str:
    return crm.get_setting(VOICE_ID_SETTING, "") or next(iter(DEFAULT_VOICES.values()))


def is_configured() -> bool:
    return bool(get_api_key())


async def synthesize(text: str) -> bytes:
    """Real ElevenLabs TTS -> MP3 bytes. Raises on any failure so the caller's
    fallback chain (Grok, then Edge) can take over -- mirrors _grok_tts's
    contract in main.py exactly."""
    api_key = get_api_key()
    if not api_key:
        raise RuntimeError("ElevenLabs not connected")
    voice_id = get_voice_id()
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    # Expressive payload first. `style` and `speed` are only honoured by newer
    # models, so if the account's model rejects them we retry once with the
    # plain settings rather than letting the whole call fail -- a hard failure
    # here silently demotes her to the free Edge voice mid-conversation, which
    # sounds like a bug to whoever's listening.
    full = {
        "text": text,
        "model_id": ELEVEN_MODEL,
        "voice_settings": {
            "stability": ELEVEN_STABILITY,
            "similarity_boost": ELEVEN_SIMILARITY,
            "style": ELEVEN_STYLE,
            "use_speaker_boost": True,
            "speed": ELEVEN_SPEED,
        },
    }
    minimal = {
        "text": text,
        "model_id": ELEVEN_MODEL,
        "voice_settings": {
            "stability": ELEVEN_STABILITY,
            "similarity_boost": ELEVEN_SIMILARITY,
        },
    }
    r = await _http.post(url, headers=headers, json=full)
    if r.status_code == 422:
        print(f"[tts] ElevenLabs rejected expressive settings, retrying plain: {r.text[:300]}", flush=True)
        r = await _http.post(url, headers=headers, json=minimal)
    if r.status_code != 200:
        raise RuntimeError(f"ElevenLabs {r.status_code}: {r.text[:600]}")
    if not r.content:
        raise ValueError("empty audio")
    return r.content
