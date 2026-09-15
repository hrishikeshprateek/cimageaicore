"""Composer settings: every key is COMPOSER_* in .env (same file as the main app settings)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from apps.api.config import REPO_ROOT

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
FONTS_DIR = ASSETS_DIR / "fonts"


class ComposerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="COMPOSER_", env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    enabled: bool = False
    template: str = "placeholder"                                   # templates_dir/<name>/template.json (placeholder is generated)
    templates_dir: Path | None = None                               # blank = <DATA_DIR>/composer/templates (DATA_DIR from the main settings / Docker)
    renders_dir: Path | None = None                                 # blank = <DATA_DIR>/renders
    font_regular: Path | None = None                                # blank = bundled Poppins (Latin + Devanagari)
    font_bold: Path | None = None
    font_fallback_regular: Path | None = None                       # blank = bundled Noto Sans Devanagari (used for runs the primary font cannot draw)
    font_fallback_bold: Path | None = None
    fribidi_lib_dir: Path | None = None                             # blank = auto (/opt/homebrew/lib on macOS); see textrender.py

    # ffmpeg export
    x264_preset: str = "medium"
    crf: int = 20
    fps: int = 30
    audio_bitrate: str = "160k"
    ffmpeg_threads: int = 0                                         # 0 = ffmpeg default
    ffmpeg_timeout_seconds: float = 900
    caption_engine: Literal["overlay", "libass"] = "overlay"        # libass needs an ffmpeg built with --enable-libass
    keep_work_files: bool = False                                   # keep caption PNGs / work dir next to the output

    # cut selection
    min_cut_seconds: float = 8.0
    max_cut_seconds: float = 60.0
    target_cut_seconds: float = 30.0
    max_cuts: int = 3
    cuts_refine: Literal["off", "auto"] = "off"                     # auto = ask Gemini (prompts/video-composer/cuts_v1.md) when the provider supports text calls
    cuts_prompt_version: str = "cuts_v1"

    # captions
    caption_max_chars_per_line: int = 34
    caption_max_lines: int = 2
    caption_min_seconds: float = 0.8

    worker_threads: int = 1

    @field_validator("templates_dir", "renders_dir", "font_regular", "font_bold", "font_fallback_regular", "font_fallback_bold", "fribidi_lib_dir", mode="before")
    @classmethod
    def _blank_is_none(cls, v):
        return None if v in ("", None) else v

    @model_validator(mode="after")
    def _resolve_dirs(self) -> "ComposerSettings":
        """Templates and renders live under the platform's DATA_DIR unless set explicitly (so Docker's /data volume keeps them)."""
        from apps.api.config import get_settings

        data_dir = get_settings().data_dir
        if self.templates_dir is None:
            self.templates_dir = data_dir / "composer" / "templates"
        if self.renders_dir is None:
            self.renders_dir = data_dir / "renders"
        return self

    @property
    def font_regular_path(self) -> Path:
        return self.font_regular or FONTS_DIR / "Poppins-Regular.ttf"

    @property
    def font_bold_path(self) -> Path:
        return self.font_bold or FONTS_DIR / "Poppins-SemiBold.ttf"

    @property
    def font_fallback_regular_path(self) -> Path:
        return self.font_fallback_regular or FONTS_DIR / "NotoSansDevanagari-Regular.ttf"

    @property
    def font_fallback_bold_path(self) -> Path:
        return self.font_fallback_bold or FONTS_DIR / "NotoSansDevanagari-Bold.ttf"

    def ensure_dirs(self) -> None:
        for d in (self.templates_dir, self.renders_dir, self.renders_dir / "records"):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_composer_settings() -> ComposerSettings:
    return ComposerSettings()
