"""ElevenLabs text-to-speech: the script's voiceover, spoken.

Hindi (Devanagari), Hinglish and English all go through the multilingual model; the voice is chosen from the account's
own voices. Audio files land under DATA_DIR/voiceovers/<script id>/ and are served from there - nothing is streamed to
a viewer's browser from ElevenLabs, so a generated line costs credits once.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger(__name__)
API = "https://api.elevenlabs.io/v1"
TIMEOUT = httpx.Timeout(20.0, read=180.0)      # a long line can take a while to synthesise


class TTSError(RuntimeError):
    pass


@dataclass
class Voice:
    id: str
    name: str
    labels: dict[str, str]
    preview_url: str | None = None
    languages: list[str] | None = None

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "labels": self.labels, "preview_url": self.preview_url, "languages": self.languages or []}


class ElevenLabsTTS:
    name = "elevenlabs"

    def __init__(self, api_key: str, *, voice_id: str = "", model: str = "eleven_multilingual_v2", output_format: str = "mp3_44100_128",
                 stability: float = 0.4, similarity: float = 0.75, style: float = 0.35, speed: float = 1.0):
        if not api_key:
            raise TTSError("no ElevenLabs API key (set ELEVENLABS_API_KEY)")
        self.api_key = api_key
        self.voice_id = voice_id
        self.model = model
        self.output_format = output_format
        self.settings = {"stability": stability, "similarity_boost": similarity, "style": style, "use_speaker_boost": True, "speed": speed}

    @property
    def _headers(self) -> dict[str, str]:
        return {"xi-api-key": self.api_key, "accept": "application/json"}

    def voices(self) -> list[Voice]:
        try:
            r = httpx.get(f"{API}/voices", headers=self._headers, timeout=TIMEOUT)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise TTSError(f"could not list ElevenLabs voices: {_why(exc)}") from exc
        out = []
        for v in r.json().get("voices", []):
            out.append(Voice(id=v.get("voice_id", ""), name=v.get("name", "?"), labels=v.get("labels") or {}, preview_url=v.get("preview_url"),
                             languages=sorted({(m or {}).get("language", "") for m in (v.get("verified_languages") or [])} - {""})))
        return out

    def speak(self, text: str, out: Path, *, voice_id: str | None = None, model: str | None = None, speed: float | None = None) -> Path:
        """Synthesise one line and write it to `out` (mp3). Returns the path."""
        vid = (voice_id or self.voice_id or "").strip()
        if not vid:
            raise TTSError("no voice chosen (set ELEVENLABS_VOICE_ID or pick a voice in the script page)")
        if not (text or "").strip():
            raise TTSError("nothing to say")
        settings = {**self.settings, **({"speed": speed} if speed else {})}
        body = {"text": text.strip(), "model_id": model or self.model, "voice_settings": settings}
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            with httpx.stream("POST", f"{API}/text-to-speech/{vid}", params={"output_format": self.output_format},
                              headers={**self._headers, "accept": "audio/mpeg", "content-type": "application/json"},
                              json=body, timeout=TIMEOUT) as r:
                if r.status_code >= 400:
                    raise TTSError(f"ElevenLabs refused the line ({r.status_code}): {r.read().decode('utf-8', 'replace')[:300]}")
                tmp = out.with_suffix(".part")
                with tmp.open("wb") as fh:
                    for chunk in r.iter_bytes():
                        fh.write(chunk)
            tmp.replace(out)
        except httpx.HTTPError as exc:
            raise TTSError(f"ElevenLabs request failed: {_why(exc)}") from exc
        log.info("tts %s -> %s (%.0f KB)", vid, out.name, out.stat().st_size / 1024)
        return out

    def usage(self) -> dict:
        """Characters left on the key - shown in the UI so nobody is surprised by a quota wall."""
        try:
            r = httpx.get(f"{API}/user/subscription", headers=self._headers, timeout=TIMEOUT)
            r.raise_for_status()
            d = r.json()
            return {"used": d.get("character_count"), "limit": d.get("character_limit"), "tier": d.get("tier")}
        except httpx.HTTPError as exc:
            log.warning("elevenlabs usage unavailable: %s", _why(exc))
            return {}


def _why(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"{exc.response.status_code} {exc.response.text[:200]}"
    return f"{type(exc).__name__}: {exc}"
