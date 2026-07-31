"""Voice chain guarantees for the ElevenLabs stage.

The speech path speaks sentence-by-sentence, so anything paid per clip
(model latency, TLS handshakes) is paid on EVERY sentence. These lock in
the two 31-Jul upgrades and the honesty contract of the fallback chain.
"""

import asyncio

import httpx
import pytest

from app import voice_eleven


def _mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30)


def test_default_model_is_flash_for_per_sentence_latency():
    """ElevenLabs' docs: flash and turbo v2.5 are functionally equivalent,
    flash is faster, flash is recommended for all use cases. Turbo here would
    silently re-add ~200ms of dead air to every spoken sentence."""
    assert voice_eleven.ELEVEN_MODEL == "eleven_flash_v2_5"


def test_shared_connection_pool_exists():
    """One AsyncClient for all clips. A per-call client = a TLS handshake per
    sentence. If this ever becomes per-call again, the voice gets slower with
    no error anywhere."""
    assert isinstance(voice_eleven._http, httpx.AsyncClient)


def test_synthesize_returns_audio_and_sends_expressive_settings(monkeypatch):
    monkeypatch.setattr(voice_eleven, "get_api_key", lambda: "xi-test")
    seen = {}

    def handler(request):
        import json
        seen.update(json.loads(request.content))
        return httpx.Response(200, content=b"MP3BYTES",
                              headers={"Content-Type": "audio/mpeg"})

    monkeypatch.setattr(voice_eleven, "_http", _mock_client(handler))
    out = asyncio.run(voice_eleven.synthesize("Hello there."))
    assert out == b"MP3BYTES"
    assert seen["model_id"] == voice_eleven.ELEVEN_MODEL
    assert "style" in seen["voice_settings"] and "speed" in seen["voice_settings"]


def test_422_retries_once_with_plain_settings(monkeypatch):
    monkeypatch.setattr(voice_eleven, "get_api_key", lambda: "xi-test")
    calls = []

    def handler(request):
        import json
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(422, text="style not supported")
        return httpx.Response(200, content=b"PLAIN", headers={"Content-Type": "audio/mpeg"})

    monkeypatch.setattr(voice_eleven, "_http", _mock_client(handler))
    out = asyncio.run(voice_eleven.synthesize("Hi."))
    assert out == b"PLAIN"
    assert len(calls) == 2
    assert "style" in calls[0]["voice_settings"]
    assert "style" not in calls[1]["voice_settings"]


def test_failure_raises_so_the_chain_can_fall_through(monkeypatch):
    """synthesize must RAISE on failure -- main.py's chain (Grok, then Edge)
    only takes over on an exception. Returning b'' would be silence."""
    monkeypatch.setattr(voice_eleven, "get_api_key", lambda: "xi-test")
    monkeypatch.setattr(voice_eleven, "_http",
                        _mock_client(lambda r: httpx.Response(500, text="boom")))
    with pytest.raises(RuntimeError):
        asyncio.run(voice_eleven.synthesize("Hi."))
    monkeypatch.setattr(voice_eleven, "get_api_key", lambda: "")
    with pytest.raises(RuntimeError):
        asyncio.run(voice_eleven.synthesize("Hi."))
