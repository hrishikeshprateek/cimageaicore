"""Environment-based configuration. Never hard-code secrets."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


# A deployment that should work with no .env editing can carry its own voiceover credentials: paste the key into
# BUILTIN_TTS_KEY below (it is one line, and it stays in whatever copy of the code you deploy). Leave it empty to require
# ELEVENLABS_API_KEY in the environment. The environment always wins over what is written here.
BUILTIN_TTS_KEY = "sk_c9e9c772be26851bdbea63525fe5470d78dc9f6bb77ab6eb"
BUILTIN_TTS_VOICE = "1qEiC6qsybMkmnNdVMbK"      # Monika Sogam - Hindi Modulated Voice (a public voice id, not a secret)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "CIMAGE AI Media Platform"
    app_version: str = "0.10.0"
    app_git_sha: str = "dev"          # stamped into the Docker image at build time (APP_GIT_SHA)
    app_build_date: str = ""

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
    max_upload_mb: int = 20480        # 20 GB: uploads stream straight to disk, and anything big is shrunk before the AI sees it
    upload_free_mb: int = 2048        # refuse an upload that would leave the disk with less than this
    worker_threads: int = 2

    institution_context: str = (
        "CIMAGE Group of Institutions, Patna, Bihar, India - a college offering "
        "BBA, BCA, MBA/PGDM and related programmes."
    )
    prompt_version: str = "v3"   # prompts/video-analysis/<version>.md - old versions are kept for comparison
    known_people_file: Path = REPO_ROOT / "prompts" / "known_people.txt"
    people_pass_version: str = "people_v1"   # blank disables the focused people pass

    # content agents (V0.5)
    blog_prompt_version: str = "blog_v2"   # v2: depth modes + pictures from video frames / the local library
    blog_model: str = ""              # blank = same model as video analysis
    blog_thinking_level: str = "medium"

    # ---- script writer (idea -> timed video script, grounded in the knowledge base)
    script_prompt_version: str = "script_v1"
    script_model: str = ""            # blank = same model as the blog writer / video analysis
    script_thinking_level: str = "medium"
    stt_provider: str = "browser"     # how the mic turns speech into text: browser (Chrome speech API) | whisper (local, later)
    stt_language: str = "hi-IN"       # dictation language the mic starts in

    # ---- word-level transcripts (precise reel cuts: sentence boundaries and the silences between them)
    transcribe_provider: str = "whisper"   # whisper | none | mock
    whisper_model: str = "large-v3-turbo"  # best Hindi/Hinglish of the fast models; 'medium' is lighter, 'tiny' for smoke tests
    whisper_device: str = "auto"
    whisper_compute_type: str = "int8"     # int8 on CPU; float16 on a GPU box
    whisper_language: str = "hi"           # Whisper decodes ONE language per window - it has no code-switching mode.
                                           # For Hindi/English mixes pin 'hi' (it keeps the English words and punctuates
                                           # both scripts); blank re-detects per file and the script then flips between runs.
    whisper_threads: int = 0               # 0 = let CTranslate2 decide
    whisper_carry_context: bool = True     # condition each window on the previous text: this is what produces full stops
    # Whisper imitates the style of this sample, so it is written the way the transcript should come out:
    # both scripts, every sentence closed (danda in Devanagari, full stop in English), institution names spelled right.
    whisper_initial_prompt: str = ("CIMAGE कॉलेज, पटना में BCA, BBA और BSc-IT की पढ़ाई होती है। "
                                   "डॉ. नीरज अग्रवाल सर ने कहा कि यह सिर्फ एक कॉलेज नहीं, एक vision है। "
                                   "Our students get placement in top IT companies, and the faculty is very supportive.")
    whisper_sentence_gap: float = 0.6       # silence that ends a sentence; Hindi speakers breathe mid-sentence
    transcribe_on_analysis: bool = True    # every analysed video gets its word timings right away

    # ---- voiceover (script -> spoken audio -> laid over the rendered video)
    tts_provider: str = "elevenlabs" if BUILTIN_TTS_KEY else "none"   # elevenlabs | none | mock
    elevenlabs_api_key: str = BUILTIN_TTS_KEY
    elevenlabs_voice_id: str = BUILTIN_TTS_VOICE   # the page can pick another from the account
    elevenlabs_model: str = "eleven_turbo_v2_5"    # speaks Hindi, half the credits of multilingual_v2
    elevenlabs_speed: float = 1.0

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
    def models_dir(self) -> Path:
        """Where local models are cached (whisper); on the data volume so a container update keeps them."""
        return self.data_dir / "models"

    @property
    def transcripts_dir(self) -> Path:
        return self.data_dir / "transcripts"

    @property
    def voiceovers_dir(self) -> Path:
        return self.data_dir / "voiceovers"

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

    nas_browse_anywhere: bool = True   # folder picker may browse the whole filesystem (off = only NAS mounts + configured roots)

    @property
    def watcher_config_file(self) -> Path:
        return self.data_dir / "watcher_config.json"

    @property
    def runtime_settings_file(self) -> Path:
        """Settings the admin UI owns (transcription + cut lengths); unset keys fall back to .env."""
        return self.data_dir / "runtime_settings.json"

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
        for d in (self.uploads_dir, self.jobs_dir, self.analyses_dir, self.proxies_dir, self.voiceovers_dir, self.transcripts_dir, self.models_dir):
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
