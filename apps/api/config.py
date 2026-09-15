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
    app_version: str = "0.9.2"

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
    embedding_provider: Literal["auto", "gemini", "ollama", "mock"] = "auto"   # ollama = local EmbeddingGemma, no API cost
    embedding_model: str = "auto"            # auto -> gemini-embedding-2 / embeddinggemma / mock-embed-v1 per provider
    embedding_dimensions: int = 768          # must match database/migrations/003_embeddings.sql
    embedding_batch_size: int = 32
    ollama_url: str = "http://localhost:11434"
    ollama_timeout_seconds: float = 120

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

    # content agents (V0.5)
    blog_prompt_version: str = "blog_v2"   # v2: depth modes + pictures from video frames / the local library
    blog_model: str = ""              # blank = same model as video analysis
    blog_thinking_level: str = "medium"

    # upload proxies: raw camera files are shrunk (720p H.264) before they go to Gemini. Cost is per second of video,
    # not per byte, so nothing is lost; files over 2 GB can't be uploaded at all without this.
    proxy_enabled: bool = True
    proxy_min_mb: int = 400
    proxy_max_height: int = 720
    proxy_max_bitrate_kbps: int = 6000
    proxy_crf: int = 28
    proxy_keep: bool = False
    proxy_timeout_seconds: int = 7200

    # folder watcher (V0.4): the NAS drops videos, the platform picks them up on its own
    watcher_enabled: bool = False
    watch_roots: str = ""                 # comma-separated; blank = every NAS_ALLOWED_ROOTS entry
    watcher_interval_seconds: float = 30
    watcher_stable_seconds: float = 10    # (size, mtime) unchanged for this long = copy finished
    watcher_max_active_jobs: int = 2      # analyses in flight at once; the rest wait their turn (Gemini quota)

    # auto-draft (V0.6): a finished video's best content opportunity becomes a blog draft for review, unasked
    auto_draft: bool = False
    auto_draft_max_per_video: int = 1
    auto_draft_min_confidence: float = 0.5
    auto_draft_depth: str = "standard"

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
        for raw in f"{self.nas_allowed_roots},{self.watch_roots}".split(","):
            raw = raw.strip()
            if not raw:
                continue
            p = Path(raw)
            roots.append((p if p.is_absolute() else REPO_ROOT / p).resolve())
        return roots

    @property
    def watcher_config_file(self) -> Path:
        return self.data_dir / "watcher_config.json"

    @property
    def watch_roots_resolved(self) -> list[Path]:
        raw = [x.strip() for x in self.watch_roots.split(",") if x.strip()]
        if not raw:
            return self.allowed_roots
        return [(Path(r) if Path(r).is_absolute() else REPO_ROOT / r).resolve() for r in raw]

    @property
    def proxies_dir(self) -> Path:
        return self.data_dir / "proxies"

    @property
    def watcher_state_file(self) -> Path:
        return self.data_dir / "watcher_state.json"

    @property
    def resolved_provider(self) -> str:
        if self.ai_provider == "auto":
            return "gemini" if self.gemini_api_key else "mock"
        return self.ai_provider

    def ensure_dirs(self) -> None:
        for d in (self.uploads_dir, self.jobs_dir, self.analyses_dir, self.proxies_dir):
            d.mkdir(parents=True, exist_ok=True)
        for d in self.allowed_roots:   # NAS mounts may be read-only or not connected yet: never fatal, the watcher skips missing roots
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                import logging

                logging.getLogger(__name__).warning("allowed root %s is not creatable (%s) - it will be used once it exists", d, exc)


@lru_cache
def get_settings() -> Settings:
    return Settings()
