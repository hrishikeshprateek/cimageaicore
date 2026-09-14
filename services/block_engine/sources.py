"""Video sources. The block engine does not care where a video came from.

    VideoSource
      ├── upload    (file sent through the web UI / API)
      ├── nas_file  (a path under an allow-listed root, e.g. the NAS mount)
      └── online    (a permitted online video - YouTube URLs, passed to Gemini directly)
"""
from __future__ import annotations

import mimetypes
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from services.ai_gateway.base import VideoInput
from services.block_engine.media import probe_duration_seconds, sha256_of
from services.block_engine.schemas import SourceInfo

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mpg", ".mpeg", ".wmv", ".3gp", ".flv"}
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}


class SourceError(ValueError):
    """Bad or disallowed source (maps to HTTP 400)."""


@dataclass
class VideoSource:
    info: SourceInfo
    path: Path | None = None
    url: str | None = None

    def to_input(self) -> VideoInput:
        return VideoInput(name=self.info.name, path=self.path, url=self.url, mime_type=self.info.mime_type)


def safe_filename(name: str) -> str:
    name = Path(name).name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "video"
    return name[:120]


def _is_video_file(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def _describe_file(path: Path, kind: str, *, compute_hash: bool = True) -> SourceInfo:
    mime, _ = mimetypes.guess_type(path.name)
    return SourceInfo(
        kind=kind,  # type: ignore[arg-type]
        name=path.name,
        path=str(path),
        mime_type=mime or "video/mp4",
        size_bytes=path.stat().st_size,
        sha256=sha256_of(path) if compute_hash else None,
        duration_seconds=probe_duration_seconds(path),
    )


def from_upload(saved_path: Path) -> VideoSource:
    if not _is_video_file(saved_path):
        raise SourceError(f"unsupported file type '{saved_path.suffix}'. Supported: {', '.join(sorted(VIDEO_EXTENSIONS))}")
    if saved_path.stat().st_size == 0:
        raise SourceError("uploaded file is empty")
    return VideoSource(info=_describe_file(saved_path, "upload"), path=saved_path)


def wait_until_stable(path: Path, stable_seconds: float, *, poll: float = 0.5, max_wait: float = 3600) -> None:
    """Block until size + mtime stop changing for `stable_seconds` (file finished copying)."""
    deadline = time.monotonic() + max_wait
    last = None
    stable_since = time.monotonic()
    while True:
        st = path.stat()
        sig = (st.st_size, st.st_mtime_ns)
        now = time.monotonic()
        if sig != last:
            last, stable_since = sig, now
        elif now - stable_since >= stable_seconds:
            return
        if now > deadline:
            raise SourceError(f"file {path.name} did not stabilise within {max_wait}s")
        time.sleep(poll)


def from_path(raw_path: str, allowed_roots: list[Path], stable_seconds: float) -> VideoSource:
    path = Path(raw_path).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise SourceError(f"file not found: {raw_path}") from exc
    if not any(resolved == root or root in resolved.parents for root in allowed_roots):
        raise SourceError(
            f"path is outside the allowed roots ({', '.join(str(r) for r in allowed_roots)}). "
            "Add the folder to NAS_ALLOWED_ROOTS to permit it."
        )
    if not resolved.is_file() or not _is_video_file(resolved):
        raise SourceError(f"not a supported video file: {resolved.name}")
    wait_until_stable(resolved, stable_seconds)
    return VideoSource(info=_describe_file(resolved, "nas_file"), path=resolved)


def from_url(raw_url: str) -> VideoSource:
    url = raw_url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in YOUTUBE_HOSTS:
        raise SourceError("only public YouTube URLs are supported as online sources in this version")
    return VideoSource(info=SourceInfo(kind="online", name=url, url=url, mime_type="video/*"), url=url)
