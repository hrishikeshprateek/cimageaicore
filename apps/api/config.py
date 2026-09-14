"""Environment-based configuration. Never hard-code secrets."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "CIMAGE AI Media Platform"
    app_version: str = "0.1.0"

    ai_provider: Literal["auto", "gemini", "mock"] = "auto"

    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"  # 13x faster than 3.8-flash on our clip, 25x the free quota, cheapest paid
    gemini_thinking_level: Literal["low", "medium", "high"] = "low"
    gemini_max_output_tokens: int | None = None
    gemini_video_resolution: Literal["low", "medium", "high", "ultra_high"] = "medium"
    gemini_video_fps: float | None = None
    gemini_delete_uploads: bool = True
    gemini_store: bool = False
    gemini_timeout_seconds: float = 1800        # the analysis call itself
    gemini_upload_timeout_seconds: float = 600   # per upload chunk
    gemini_poll_timeout_seconds: float = 30      # status polls / deletes

    # embeddings (V0.3)
    embedding_provider: Literal["auto", "gemini", "mock"] = "auto"
    embedding_model: str = "gemini-embedding-2"
    embedding_dimensions: int = 768          # must match database/migrations/003_embeddings.sql
    embedding_batch_size: int = 32

    data_dir: Path = REPO_ROOT / "data"
    nas_allowed_roots: str = "./data/nas-test"
    stable_seconds: float = 2.0
    max_upload_mb: int = 2048
    worker_threads: int = 2

    institution_context: str = (
        "CIMAGE Group of Institutions, Patna, Bihar, India - a college offering "
        "BBA, BCA, MBA/PGDM and related programmes."
    )
    prompt_version: str = "v2"   # prompts/video-analysis/<version>.md - old versions are kept for comparison
    known_people_file: Path = REPO_ROOT / "prompts" / "known_people.txt"
    people_pass_version: str = "people_v1"   # blank disables the focused people pass

    database_url: str = ""      # blank -> JSON-file job store
    auto_migrate: bool = True
    redis_url: str = ""         # used from V0.4

    @field_validator("gemini_max_output_tokens", "gemini_video_fps", mode="before")
    @classmethod
    def _blank_is_none(cls, v):
        return None if v in ("", None) else v

    # ---- derived paths -------------------------------------------------
    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def analyses_dir(self) -> Path:
        return self.data_dir / "analyses"

    @property
    def allowed_roots(self) -> list[Path]:
        roots = []
        for raw in self.nas_allowed_roots.split(","):
            raw = raw.strip()
            if not raw:
                continue
            p = Path(raw)
            roots.append((p if p.is_absolute() else REPO_ROOT / p).resolve())
        return roots

    @property
    def resolved_provider(self) -> str:
        if self.ai_provider == "auto":
            return "gemini" if self.gemini_api_key else "mock"
        return self.ai_provider

    def ensure_dirs(self) -> None:
        for d in (self.uploads_dir, self.jobs_dir, self.analyses_dir, *self.allowed_roots):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
