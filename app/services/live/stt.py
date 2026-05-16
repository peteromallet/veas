"""Streaming STT interface + a Stub impl + an OpenAI Realtime impl.

Contract (``StreamingTranscriber``):

* ``start()`` — open whatever upstream connection is needed.
* ``push(pcm: bytes)`` — non-blocking; queue the frame for transcription.
* ``aclose()`` — flush + close.

Events are delivered via an ``asyncio.Queue`` of typed dicts:

* ``{"type": "partial", "text": "…", "ts": 1731....}`` — interim hypothesis
* ``{"type": "final",   "text": "…", "ts": 1731....}`` — finalized turn
* ``{"type": "error",   "message": "…"}``               — non-fatal

The WS handler in ``app/routers/live_voice.py`` forwards these events to
the client and persists every ``final`` to ``mediator.transcript_turns``.

This module ships two impls:

* :class:`StubTranscriber` — deterministic events on a timer; powers
  browser-without-mic dev runs and the no-key local stack.
* :class:`OpenAIRealtimeTranscriber` — wraps the ``gpt-4o-mini-transcribe``
  Realtime WS endpoint. Selected when ``OPENAI_API_KEY`` is set AND
  ``LIVE_VOICE_STT_PROVIDER`` is unset or ``=openai``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from typing import Any, AsyncIterator, Protocol

import httpx

logger = logging.getLogger(__name__)


class StreamingTranscriber(Protocol):
    events: asyncio.Queue[dict[str, Any]]

    async def start(self) -> None: ...
    async def push(self, pcm: bytes) -> None: ...
    async def flush(self) -> None: ...
    async def aclose(self) -> None: ...


def select_transcriber(*, target_sample_rate: int = 16000) -> StreamingTranscriber:
    """Pick the STT impl based on env.

    * ``LIVE_VOICE_STT_PROVIDER=stub`` (or no OpenAI key) → :class:`StubTranscriber`.
    * ``LIVE_VOICE_STT_PROVIDER=whisper`` (default when key set) →
      :class:`WhisperBufferedTranscriber` — accumulates PCM, flushes on
      VAD turn_end or after a max-buffer timer to OpenAI Whisper-1.
    * ``LIVE_VOICE_STT_PROVIDER=openai_realtime`` →
      :class:`OpenAIRealtimeTranscriber` (kept for backward-compat).
    """
    provider = (os.environ.get("LIVE_VOICE_STT_PROVIDER") or "").strip().lower()
    has_real_key = bool(
        (os.environ.get("OPENAI_API_KEY") or "").startswith("sk-")
        and "stub" not in (os.environ.get("OPENAI_API_KEY") or "")
    )
    if provider == "stub" or (provider == "" and not has_real_key):
        return StubTranscriber()
    if provider == "openai_realtime":
        return OpenAIRealtimeTranscriber(sample_rate=target_sample_rate)
    return WhisperBufferedTranscriber(sample_rate=target_sample_rate)


# --------------------------------------------------------------------------- #
# Stub impl.
# --------------------------------------------------------------------------- #


class StubTranscriber:
    """Emits a fake partial + final pair every ~2 seconds of audio.

    Useful for dev runs where the OpenAI key is missing or the headless
    browser produces silence frames. The wire protocol exercised here is
    identical to the real transcriber.
    """

    def __init__(self) -> None:
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
        self._task: asyncio.Task[None] | None = None
        self._stopped = False
        self._bytes_seen = 0
        self._last_final = 0.0
        self._turn_counter = 0

    async def start(self) -> None:
        # No upstream to open.
        pass

    async def push(self, pcm: bytes) -> None:
        if self._stopped:
            return
        self._bytes_seen += len(pcm)
        now = time.time()
        if now - self._last_final >= 2.0 and self._bytes_seen >= 2 * 16000 * 2:
            # ~2 seconds of 16kHz int16 audio.
            self._turn_counter += 1
            await self._safe_emit({
                "type": "partial",
                "text": f"(stub partial #{self._turn_counter})",
                "ts": now,
            })
            await self._safe_emit({
                "type": "final",
                "text": f"This is stub transcript line {self._turn_counter}.",
                "ts": now,
            })
            self._last_final = now
            self._bytes_seen = 0

    async def flush(self) -> None:
        # No-op for the stub.
        return None

    async def aclose(self) -> None:
        self._stopped = True

    async def _safe_emit(self, event: dict[str, Any]) -> None:
        try:
            self.events.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("stub stt: event queue full; dropping %s", event.get("type"))


# --------------------------------------------------------------------------- #
# Real impl: OpenAI Whisper-1 over HTTP, buffered + flushed on VAD turn_end.
#
# Why this instead of the Realtime WS path: Whisper-1 is a single
# request/response — far simpler to make robust, no upstream WS to keep
# alive, no audio-format dance. We buffer PCM client-side and flush each
# user turn as a WAV file. Latency is ~ASR_LATENCY = network + Whisper
# processing time (~500ms-2s for a 5s clip), which fits the SLO budget
# at p95 ≤ 2000ms for the full ear-to-ear path.
# --------------------------------------------------------------------------- #


def _wav_header(num_samples: int, sample_rate: int, channels: int = 1, bits_per_sample: int = 16) -> bytes:
    """Build a 44-byte WAV (RIFF) header for raw PCM int16 audio."""
    import struct
    byte_rate = sample_rate * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8
    data_size = num_samples * channels * bits_per_sample // 8
    return (
        b"RIFF"
        + struct.pack("<I", 36 + data_size)
        + b"WAVE"
        + b"fmt "
        + struct.pack("<I", 16)
        + struct.pack("<H", 1)  # PCM
        + struct.pack("<H", channels)
        + struct.pack("<I", sample_rate)
        + struct.pack("<I", byte_rate)
        + struct.pack("<H", block_align)
        + struct.pack("<H", bits_per_sample)
        + b"data"
        + struct.pack("<I", data_size)
    )


_WHISPER_URL = "https://api.openai.com/v1/audio/transcriptions"
_MIN_FLUSH_BYTES = 16000 * 2 * 1  # 1 second of 16 kHz int16 PCM
_MAX_BUFFER_BYTES = 16000 * 2 * 12  # 12 seconds — force-flush ceiling

# Known Whisper hallucinations on silence — strip these before emitting
# a final.  The Russian one (DimaTorzok) shows up on roughly every
# 5th silent flush; the Korean one likewise.
_WHISPER_HALLUCINATIONS = (
    "субтитры предоставил",
    "dimatorzok",
    "thanks for watching",
    "thank you for watching",
    "사용자에게 자막을",
    "韩国语字幕",
)


def _rms_below_threshold(pcm_blob: bytes, *, threshold: float) -> bool:
    """Compute RMS of an int16 PCM blob; True if below threshold (0..1)."""
    import array
    if not pcm_blob:
        return True
    samples = array.array("h")
    samples.frombytes(pcm_blob)
    if not samples:
        return True
    # Mean square in int^2 space, then sqrt + normalize to [0,1] by 32768.
    s = 0
    for v in samples:
        s += v * v
    mean_sq = s / len(samples)
    rms = (mean_sq ** 0.5) / 32768.0
    return rms < threshold


def _looks_like_hallucination(text: str) -> bool:
    low = text.lower().strip()
    if not low:
        return True
    for marker in _WHISPER_HALLUCINATIONS:
        if marker in low:
            return True
    return False


class WhisperBufferedTranscriber:
    """Buffer PCM frames; flush each user turn to OpenAI Whisper-1.

    Push() accumulates raw int16 PCM in memory. flush() (called by the
    WS handler on a VAD `turn_end` control frame, or periodically by
    the auto-flush watchdog) encodes the buffer as a WAV blob, POSTs
    to ``/v1/audio/transcriptions``, and emits a `final` event with
    the returned text.
    """

    def __init__(self, *, sample_rate: int = 16000) -> None:
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=128)
        self._sample_rate = sample_rate
        self._buf = bytearray()
        self._lock = asyncio.Lock()
        self._stopped = False
        self._watchdog_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        # Watchdog auto-flushes when the buffer overflows (caller didn't
        # send a turn_end signal but the user has been talking for a long
        # stretch).
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    async def push(self, pcm: bytes) -> None:
        if self._stopped:
            return
        async with self._lock:
            self._buf.extend(pcm)
            if len(self._buf) >= _MAX_BUFFER_BYTES:
                # Detach + flush asynchronously so we don't block the
                # binary-frame receiver.
                blob = bytes(self._buf)
                self._buf.clear()
                asyncio.create_task(self._transcribe(blob))

    async def flush(self) -> None:
        if self._stopped:
            return
        async with self._lock:
            if len(self._buf) < _MIN_FLUSH_BYTES:
                return
            blob = bytes(self._buf)
            self._buf.clear()
        await self._transcribe(blob)

    async def aclose(self) -> None:
        self._stopped = True
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        # Final flush so the last user turn isn't lost.
        async with self._lock:
            blob = bytes(self._buf) if len(self._buf) >= _MIN_FLUSH_BYTES else b""
            self._buf.clear()
        if blob:
            await self._transcribe(blob)

    async def _watchdog_loop(self) -> None:
        try:
            while not self._stopped:
                await asyncio.sleep(2.0)
                async with self._lock:
                    if len(self._buf) >= _MAX_BUFFER_BYTES:
                        blob = bytes(self._buf)
                        self._buf.clear()
                    else:
                        continue
                await self._transcribe(blob)
        except asyncio.CancelledError:
            return

    async def _transcribe(self, pcm_blob: bytes) -> None:
        api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
        if not api_key.startswith("sk-") or "stub" in api_key:
            await self._safe_emit({"type": "error", "message": "OPENAI_API_KEY missing or stub"})
            return

        # Energy gate: if the entire buffer is below a minimum RMS, skip
        # the Whisper call.  Whisper hallucinates plausible-but-wrong text
        # (e.g. "Субтитры предоставил DimaTorzok" — Russian subtitle credit)
        # on pure silence/noise, and those costs add up.
        if _rms_below_threshold(pcm_blob, threshold=0.0035):
            await self._safe_emit({"type": "partial", "text": "(silence)", "ts": time.time()})
            return

        num_samples = len(pcm_blob) // 2
        wav_bytes = _wav_header(num_samples, self._sample_rate) + pcm_blob

        files = {"file": ("audio.wav", wav_bytes, "audio/wav")}
        data = {
            "model": os.environ.get("LIVE_VOICE_WHISPER_MODEL") or "whisper-1",
            "response_format": "json",
            "temperature": "0",
            # Force English unless the operator overrides — kills the
            # Whisper Russian/Korean subtitle hallucinations on silence.
            "language": os.environ.get("LIVE_VOICE_WHISPER_LANGUAGE") or "en",
            # Optional grounding prompt to bias the model toward English
            # conversational speech rather than music/subtitle templates.
            "prompt": (
                "This is a live one-on-one English-language coaching conversation. "
                "Transcribe only what the speaker actually says. Output empty on silence."
            ),
        }
        headers = {"Authorization": f"Bearer {api_key}"}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
                resp = await client.post(_WHISPER_URL, headers=headers, files=files, data=data)
        except Exception as exc:
            await self._safe_emit({"type": "error", "message": f"whisper request failed: {exc}"})
            return
        if resp.status_code >= 400:
            await self._safe_emit({
                "type": "error",
                "message": f"whisper status={resp.status_code} body={resp.text[:200]}",
            })
            return
        try:
            payload = resp.json()
            text = (payload.get("text") or "").strip()
        except Exception as exc:
            await self._safe_emit({"type": "error", "message": f"whisper response parse failed: {exc}"})
            return
        if not text or _looks_like_hallucination(text):
            # Whisper returned empty / known hallucination on silence.
            await self._safe_emit({"type": "partial", "text": "(silence)", "ts": time.time()})
            return
        await self._safe_emit({"type": "final", "text": text, "ts": time.time()})

    async def _safe_emit(self, event: dict[str, Any]) -> None:
        try:
            self.events.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("whisper stt: event queue full; dropping %s", event.get("type"))


# --------------------------------------------------------------------------- #
# Real impl: OpenAI Realtime gpt-4o-mini-transcribe over WSS.
# --------------------------------------------------------------------------- #


_OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime?intent=transcription"


class OpenAIRealtimeTranscriber:
    """Connect to OpenAI Realtime and stream PCM frames for transcription.

    Audio frames are 16 kHz mono int16, sent as base64-encoded chunks via
    ``input_audio_buffer.append`` events. Partial transcripts arrive as
    ``conversation.item.input_audio_transcription.delta``; finals as
    ``conversation.item.input_audio_transcription.completed``.

    Failures are surfaced as ``{"type": "error", …}`` events; the WS
    handler decides whether to fall back to the stub or close the session.
    """

    def __init__(self, *, sample_rate: int = 16000, model: str = "gpt-4o-mini-transcribe") -> None:
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=128)
        self._sample_rate = sample_rate
        self._model = model
        self._ws: Any = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stopped = False

    async def start(self) -> None:
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover - dep declared at module level
            await self._safe_emit({"type": "error", "message": f"websockets not installed: {exc}"})
            self._stopped = True
            return

        api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
        if not api_key.startswith("sk-") or "stub" in api_key:
            await self._safe_emit({"type": "error", "message": "OPENAI_API_KEY missing or stub"})
            self._stopped = True
            return

        headers = [
            ("Authorization", f"Bearer {api_key}"),
            ("OpenAI-Beta", "realtime=v1"),
        ]
        try:
            self._ws = await websockets.connect(_OPENAI_REALTIME_URL, additional_headers=headers)
        except Exception as exc:
            await self._safe_emit({"type": "error", "message": f"openai connect failed: {exc}"})
            self._stopped = True
            return

        await self._ws.send(json.dumps({
            "type": "transcription_session.update",
            "session": {
                "input_audio_format": "pcm16",
                "input_audio_sample_rate_hz": self._sample_rate,
                "input_audio_transcription": {"model": self._model},
                "turn_detection": {"type": "server_vad"},
            },
        }))
        self._reader_task = asyncio.create_task(self._reader_loop())

    async def push(self, pcm: bytes) -> None:
        if self._stopped or self._ws is None:
            return
        try:
            await self._ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm).decode("ascii"),
            }))
        except Exception as exc:
            await self._safe_emit({"type": "error", "message": f"openai push failed: {exc}"})

    async def flush(self) -> None:
        # Realtime path commits the buffer to force a transcription.
        if self._stopped or self._ws is None:
            return
        try:
            await self._ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        except Exception:
            pass

    async def aclose(self) -> None:
        self._stopped = True
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def _reader_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    payload = json.loads(raw)
                except Exception:
                    continue
                kind = payload.get("type")
                text = payload.get("delta") or payload.get("transcript") or ""
                if kind == "conversation.item.input_audio_transcription.delta" and text:
                    await self._safe_emit({"type": "partial", "text": text, "ts": time.time()})
                elif kind == "conversation.item.input_audio_transcription.completed" and text:
                    await self._safe_emit({"type": "final", "text": text, "ts": time.time()})
                elif kind == "error":
                    await self._safe_emit({"type": "error", "message": str(payload.get("error"))})
        except asyncio.CancelledError:
            return
        except Exception as exc:
            await self._safe_emit({"type": "error", "message": f"openai reader crashed: {exc}"})

    async def _safe_emit(self, event: dict[str, Any]) -> None:
        try:
            self.events.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("openai stt: event queue full; dropping %s", event.get("type"))


# --------------------------------------------------------------------------- #
# Helper: drain events into an async iterator.
# --------------------------------------------------------------------------- #


async def drain_events(transcriber: StreamingTranscriber) -> AsyncIterator[dict[str, Any]]:
    """Yield events from the transcriber. Caller decides when to stop."""
    while True:
        event = await transcriber.events.get()
        yield event
