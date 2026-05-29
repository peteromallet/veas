"""Whisper hardening tests (Sprint 4)."""

from __future__ import annotations

import struct

from app.services.live.stt import (
    GroqWhisperTranscriber,
    WhisperBufferedTranscriber,
    _looks_like_hallucination,
    _rms_below_threshold,
    select_transcriber,
)


# --------------------------------------------------------------------------- #
# Hallucination filter — strips known Whisper-on-silence templates.
# --------------------------------------------------------------------------- #


class TestHallucinationFilter:
    def test_strips_russian_subtitle_credit(self) -> None:
        assert _looks_like_hallucination("Субтитры предоставил DimaTorzok") is True

    def test_strips_thanks_for_watching(self) -> None:
        assert _looks_like_hallucination("Thanks for watching.") is True
        assert _looks_like_hallucination("THANK YOU FOR WATCHING!") is True

    def test_strips_dimatorzok_inline(self) -> None:
        # Mixed-in form Whisper occasionally produces.
        assert _looks_like_hallucination("Hello DimaTorzok thanks") is True

    def test_strips_prompt_echo(self) -> None:
        assert (
            _looks_like_hallucination("Transcribe only what the speaker says.")
            is True
        )
        assert _looks_like_hallucination("Output empty on silence.") is True

    def test_strips_output_silence_artifact(self) -> None:
        assert _looks_like_hallucination("Output.") is True

    def test_lets_real_speech_through(self) -> None:
        assert _looks_like_hallucination("I'm leaning toward fall") is False
        assert _looks_like_hallucination("Let me think about that") is False
        assert _looks_like_hallucination("This is hard to talk about") is False

    def test_empty_text_is_hallucination(self) -> None:
        assert _looks_like_hallucination("") is True
        assert _looks_like_hallucination("   ") is True


def test_select_transcriber_uses_settings_provider(monkeypatch) -> None:
    """Local .env-backed settings must not fall through to StubTranscriber."""

    monkeypatch.delenv("LIVE_VOICE_STT_PROVIDER", raising=False)
    monkeypatch.setenv("LIVE_VOICE_STT_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-real-looking")
    from app.config import get_settings

    get_settings.cache_clear()

    transcriber = select_transcriber()

    assert isinstance(transcriber, GroqWhisperTranscriber)


def test_groq_transcriber_reads_settings_api_key(monkeypatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "gsk-settings-key")
    from app.config import get_settings

    get_settings.cache_clear()

    transcriber = GroqWhisperTranscriber()

    assert transcriber._provider_api_key() == "gsk-settings-key"


def test_openai_transcriber_reads_settings_api_key(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-settings-key")
    from app.config import get_settings

    get_settings.cache_clear()

    transcriber = WhisperBufferedTranscriber()

    assert transcriber._provider_api_key() == "sk-settings-key"


# --------------------------------------------------------------------------- #
# Silence gate — RMS-based energy detection.
# --------------------------------------------------------------------------- #


class TestSilenceGate:
    def test_zero_pcm_is_silent(self) -> None:
        pcm = b"\x00\x00" * 16000
        assert _rms_below_threshold(pcm, threshold=0.0035) is True

    def test_loud_sine_is_not_silent(self) -> None:
        # Generate a 440 Hz sine at peak ~24576 (75% of int16 max).
        import math
        samples = []
        for i in range(16000):
            v = int(24576 * math.sin(2 * math.pi * 440 * i / 16000))
            samples.append(v)
        pcm = struct.pack("<" + "h" * len(samples), *samples)
        assert _rms_below_threshold(pcm, threshold=0.0035) is False

    def test_empty_pcm_is_silent(self) -> None:
        assert _rms_below_threshold(b"", threshold=0.0035) is True

    def test_quiet_room_tone_is_silent(self) -> None:
        # Random +/- 5 LSB noise — well below threshold.
        import random
        random.seed(42)
        samples = [random.randint(-5, 5) for _ in range(16000)]
        pcm = struct.pack("<" + "h" * len(samples), *samples)
        assert _rms_below_threshold(pcm, threshold=0.0035) is True
