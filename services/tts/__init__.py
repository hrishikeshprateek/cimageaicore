"""Voiceover providers. ElevenLabs for real audio; a mock that writes silence of the right length for tests."""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from services.tts.elevenlabs import ElevenLabsTTS, TTSError, Voice

log = logging.getLogger(__name__)

# How long a line takes to say, per language, at the pace the script agent writes to (words per second).
SPEAKING_RATE = {"hi": 2.1, "hinglish": 2.3, "en": 2.5}


def spoken_seconds(text: str, language: str = "hi") -> float:
    words = len([w for w in (text or "").split() if w])
    return round(words / SPEAKING_RATE.get(language, 2.2), 2)


class MockTTS:
    """Silence of about the right duration - the pipeline (durations, mixing, the player) is exercised without credits."""

    name = "mock"

    def __init__(self, voice_id: str = "mock-voice", **_):
        self.voice_id = voice_id

    def voices(self) -> list[Voice]:
        return [Voice(id="mock-voice", name="[MOCK] Narrator", labels={"accent": "indian"}, languages=["hi", "en"]),
                Voice(id="mock-voice-2", name="[MOCK] Presenter", labels={"accent": "indian"}, languages=["hi"])]

    def speak(self, text: str, out: Path, *, voice_id: str | None = None, model: str | None = None, speed: float | None = None) -> Path:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise TTSError("ffmpeg not found on PATH (the mock voice needs it to write silence)")
        out.parent.mkdir(parents=True, exist_ok=True)
        seconds = max(0.6, spoken_seconds(text))
        proc = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                               "-t", f"{seconds:.2f}", "-c:a", "libmp3lame", "-b:a", "96k", str(out)], capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise TTSError(f"mock voice failed: {proc.stderr[-200:]}")
        return out

    def usage(self) -> dict:
        return {"used": 0, "limit": None, "tier": "mock"}


def build_tts(settings):
    """The configured voiceover provider, or None when voiceovers are off / unconfigured."""
    provider = (getattr(settings, "tts_provider", "") or "none").lower()
    if provider in ("", "none", "off"):
        return None
    if provider == "mock":
        return MockTTS(voice_id=settings.elevenlabs_voice_id or "mock-voice")
    if provider == "elevenlabs":
        if not settings.elevenlabs_api_key:
            log.warning("TTS_PROVIDER=elevenlabs but ELEVENLABS_API_KEY is empty - voiceovers stay off")
            return None
        return ElevenLabsTTS(settings.elevenlabs_api_key, voice_id=settings.elevenlabs_voice_id, model=settings.elevenlabs_model,
                             speed=settings.elevenlabs_speed)
    log.warning("unknown TTS_PROVIDER=%s - voiceovers stay off", provider)
    return None


__all__ = ["ElevenLabsTTS", "MockTTS", "TTSError", "Voice", "build_tts", "spoken_seconds", "SPEAKING_RATE"]
