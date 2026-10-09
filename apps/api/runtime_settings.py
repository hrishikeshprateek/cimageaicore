"""Settings the admin UI owns, persisted in data/runtime_settings.json and applied over the .env values.

Only the knobs that genuinely need tuning while the platform runs live here - how speech is turned into
sentences, and how long a reel may be - because both decide where a cut is allowed to end. Everything
else stays in .env. A field left unset inherits the .env value, exactly like an unset active prompt
version falls back to the bundled default.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

log = logging.getLogger(__name__)

# Whisper decodes one language per window and has no code-switching mode, so this is a real choice
# rather than a free-text field: 'hi' is what keeps Hindi-English speech coherent.
LANGUAGES = [
    ("hi", "Hindi (keeps English words in Latin script) - best for Hindi/English mixes"),
    ("en", "English only"),
    ("", "Detect per video (the script can change between runs)"),
]


class RuntimeSettings(BaseModel):
    """`None` means "inherit from .env"."""

    # ---- transcription: how speech becomes the sentences a cut ends on
    whisper_language: str | None = None
    whisper_carry_context: bool | None = None
    whisper_sentence_gap: float | None = Field(default=None, ge=0.1, le=3.0)
    whisper_initial_prompt: str | None = None

    # ---- reel cuts
    min_cut_seconds: float | None = Field(default=None, ge=1.0, le=600.0)
    max_cut_seconds: float | None = Field(default=None, ge=2.0, le=600.0)
    target_cut_seconds: float | None = Field(default=None, ge=2.0, le=600.0)
    max_cuts: int | None = Field(default=None, ge=1, le=20)

    @model_validator(mode="after")
    def _lengths_make_sense(self) -> "RuntimeSettings":
        if self.whisper_language is not None and self.whisper_language not in {code for code, _ in LANGUAGES}:
            raise ValueError(f"language must be one of {', '.join(repr(c) for c, _ in LANGUAGES)}")
        if self.whisper_initial_prompt is not None and len(self.whisper_initial_prompt) > 800:
            raise ValueError("the initial prompt is a short sample, not a document (800 characters max)")
        lo, hi, target = self.min_cut_seconds, self.max_cut_seconds, self.target_cut_seconds
        if lo is not None and hi is not None and lo >= hi:
            raise ValueError("the shortest cut must be shorter than the longest one")
        if target is not None and lo is not None and target < lo:
            raise ValueError("the target length cannot be below the shortest cut")
        if target is not None and hi is not None and target > hi:
            raise ValueError("the target length cannot be above the longest cut")
        return self

    def set_values(self) -> dict[str, Any]:
        """Only the fields the UI has actually set - what gets written to disk."""
        return {k: v for k, v in self.model_dump().items() if v is not None}


def load(path: Path) -> RuntimeSettings:
    if not path.exists():
        return RuntimeSettings()
    try:
        return RuntimeSettings.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - a corrupt file must not stop the platform booting
        log.warning("ignoring unreadable %s (%s) - using the .env values", path.name, exc)
        return RuntimeSettings()


def save(path: Path, cfg: RuntimeSettings) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg.set_values(), indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------------------------
# applying
# --------------------------------------------------------------------------------------------

TRANSCRIBE_FIELDS = ("whisper_language", "whisper_carry_context", "whisper_sentence_gap", "whisper_initial_prompt")
CUT_FIELDS = ("min_cut_seconds", "max_cut_seconds", "target_cut_seconds", "max_cuts")


def check_against(cfg: RuntimeSettings, composer_settings) -> None:
    """Validate the set values against what they will actually combine with.

    The model's own cross-field check can only compare fields the UI set together; a single value saved on
    its own (a 500 s shortest cut against a 60 s longest one from .env) has to be checked against the
    effective configuration, which is what the cutter will really see.
    """
    def eff(field: str) -> float:
        v = getattr(cfg, field)
        return float(v if v is not None else getattr(composer_settings, field))

    lo, hi, target = eff("min_cut_seconds"), eff("max_cut_seconds"), eff("target_cut_seconds")
    if lo >= hi:
        raise ValueError(f"the shortest cut ({lo:g}s) must be shorter than the longest ({hi:g}s)")
    if not lo <= target <= hi:
        raise ValueError(f"the preferred length ({target:g}s) must sit between the shortest ({lo:g}s) and longest ({hi:g}s) cut")


def apply(cfg: RuntimeSettings, settings, composer_settings) -> None:
    """Write the set values onto the live settings objects. Both are plain pydantic models at runtime,
    so this is what the rest of the app reads from on the next request."""
    for f in TRANSCRIBE_FIELDS:
        v = getattr(cfg, f)
        if v is not None:
            setattr(settings, f, v)
    for f in CUT_FIELDS:
        v = getattr(cfg, f)
        if v is not None:
            setattr(composer_settings, f, v)
