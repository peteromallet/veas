"""Streaming TTS provider — ElevenLabs Flash (Sprint 3 / 3b).

Contract (:class:`TtsProvider`):

* ``synthesize_mp3(text)`` — async generator yielding mp3 chunks.
* Caller writes them to a file or pipes them straight to the client.

Ships two impls:

* :class:`StubTtsProvider` — returns an empty stream so the WS handler
  can publish a `bot_audio_unavailable` event and the frontend falls back
  to browser SpeechSynthesis without surprise.
* :class:`ElevenLabsFlashTtsProvider` — calls
  `POST /v1/text-to-speech/{voice_id}/stream` with model_id
  `eleven_flash_v2_5` when ``ELEVENLABS_API_KEY`` is set.

:func:`select_tts_provider` picks based on env.
"""

from __future__ import annotations

import logging
import os
from typing import AsyncIterator, Protocol

import httpx

logger = logging.getLogger(__name__)

_ELEVENLABS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
DEFAULT_VOICE_ID = "EXAVITQu4vr4xnSDxMaL"  # ElevenLabs "Bella" — placeholder.


class TtsProvider(Protocol):
    name: str

    async def synthesize_mp3(self, text: str) -> AsyncIterator[bytes]: ...


def select_tts_provider() -> "TtsProvider":
    """Pick the TTS impl based on env.

    * `LIVE_VOICE_TTS_PROVIDER=stub` (or missing key) → :class:`StubTtsProvider`.
    * `LIVE_VOICE_TTS_PROVIDER=elevenlabs` (default when key set) →
      :class:`ElevenLabsFlashTtsProvider`.
    """
    provider = (os.environ.get("LIVE_VOICE_TTS_PROVIDER") or "").strip().lower()
    api_key = (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
    has_real_key = bool(api_key) and "stub" not in api_key
    if provider == "stub" or (provider == "" and not has_real_key):
        return StubTtsProvider()
    return ElevenLabsFlashTtsProvider()


class StubTtsProvider:
    """No-op stream — the WS handler interprets an empty stream as
    "fall back to browser SpeechSynthesis".
    """

    name = "stub"

    async def synthesize_mp3(self, text: str) -> AsyncIterator[bytes]:  # type: ignore[override]
        if False:  # pragma: no cover - intentional empty async generator
            yield b""


class ElevenLabsFlashTtsProvider:
    """Streams mp3 from `eleven_flash_v2_5`.

    Per-turn cost is tracked at the call site (see budget guard).
    """

    name = "elevenlabs_flash"

    def __init__(self, *, voice_id: str | None = None, model_id: str = "eleven_flash_v2_5") -> None:
        self._voice_id = voice_id or os.environ.get("ELEVENLABS_VOICE_ID") or DEFAULT_VOICE_ID
        self._model_id = model_id

    async def synthesize_mp3(self, text: str) -> AsyncIterator[bytes]:  # type: ignore[override]
        api_key = (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
        if not api_key or "stub" in api_key:
            logger.warning("elevenlabs: API key missing/stub; yielding empty stream")
            return
        url = _ELEVENLABS_URL.format(voice_id=self._voice_id)
        headers = {
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }
        body = {
            "text": text,
            "model_id": self._model_id,
            "output_format": "mp3_44100_128",
            "voice_settings": {
                "stability": 0.45,
                "similarity_boost": 0.75,
                "style": 0.1,
                "use_speaker_boost": True,
            },
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
                async with client.stream("POST", url, headers=headers, json=body) as resp:
                    if resp.status_code >= 400:
                        text_body = await resp.aread()
                        logger.warning(
                            "elevenlabs: status=%s body=%s",
                            resp.status_code,
                            text_body[:200],
                        )
                        return
                    async for chunk in resp.aiter_bytes(chunk_size=4096):
                        if chunk:
                            yield chunk
        except Exception:
            logger.exception("elevenlabs: stream failed")
            return
