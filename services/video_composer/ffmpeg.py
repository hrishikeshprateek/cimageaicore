"""ffmpeg / ffprobe access for the composer: capabilities, probing, running with a timeout."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel

log = logging.getLogger(__name__)

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


class MediaInfo(BaseModel):
    path: str
    width: int
    height: int
    duration: float
    fps: float | None = None
    has_audio: bool = False
    rotation: int = 0
    video_codec: str | None = None
    size_bytes: int | None = None


class FFmpegError(RuntimeError):
    pass


@lru_cache
def capabilities() -> dict:
    """What this ffmpeg build can do - decides caption engine / encoder choices and is shown in /composer/system."""
    if FFMPEG is None:
        return {"ffmpeg": None, "ffprobe": FFPROBE, "filters": [], "encoders": [], "version": None}

    def names(kind: str, col: int) -> set[str]:
        out = subprocess.run([FFMPEG, "-hide_banner", f"-{kind}"], capture_output=True, text=True, check=False, timeout=30).stdout
        found = set()
        for ln in out.splitlines():
            parts = ln.split()
            if len(parts) > col and parts[0] and not parts[0].isalpha() and parts[0] != "=":
                found.add(parts[col])
        return found

    version = subprocess.run([FFMPEG, "-version"], capture_output=True, text=True, check=False, timeout=30).stdout.splitlines()[:1]
    filters = names("filters", 1)
    encoders = names("encoders", 1)
    wanted_f = ["overlay", "scale", "pad", "crop", "fps", "format", "subtitles", "drawtext", "afade"]
    wanted_e = ["libx264", "h264_videotoolbox", "h264_nvenc", "aac", "prores_ks", "libvpx-vp9"]
    return {
        "ffmpeg": FFMPEG,
        "ffprobe": FFPROBE,
        "version": version[0] if version else None,
        "filters": sorted(f for f in wanted_f if f in filters),
        "encoders": sorted(e for e in wanted_e if e in encoders),
        "libass": "subtitles" in filters,
        "drawtext": "drawtext" in filters,
    }


def video_encoder() -> tuple[str, list[str]]:
    enc = set(capabilities()["encoders"])
    if "libx264" in enc:
        return "libx264", []
    if "h264_videotoolbox" in enc:
        return "h264_videotoolbox", ["-b:v", "8M"]
    if "h264_nvenc" in enc:
        return "h264_nvenc", []
    return "mpeg4", ["-q:v", "3"]


def probe(path: Path) -> MediaInfo:
    if FFPROBE is None:
        raise FFmpegError("ffprobe not found on PATH")
    if not path.exists():
        raise FFmpegError(f"file not found: {path}")
    proc = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "stream=index,codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate,side_data_list:stream_tags=rotate:format=duration,size",
         "-of", "json", str(path)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    if proc.returncode != 0:
        raise FFmpegError(f"ffprobe failed for {path.name}: {proc.stderr.strip()[-300:]}")
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if v is None:
        raise FFmpegError(f"no video stream in {path.name}")
    rotation = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            try:
                rotation = int(round(float(sd["rotation"])))
            except (TypeError, ValueError):
                pass
    if not rotation and v.get("tags", {}).get("rotate"):
        try:
            rotation = int(v["tags"]["rotate"])
        except ValueError:
            pass
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    if abs(rotation) % 180 == 90:      # ffmpeg auto-rotates on decode, so the effective frame is swapped
        w, h = h, w
    fps = None
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = v.get(key) or ""
        if "/" in raw:
            num, den = raw.split("/")
            if float(den or 0):
                fps = float(num) / float(den)
                break
    dur = float(data.get("format", {}).get("duration") or 0)
    size = data.get("format", {}).get("size")
    return MediaInfo(path=str(path), width=w, height=h, duration=dur, fps=fps, has_audio=any(s.get("codec_type") == "audio" for s in streams),
                     rotation=rotation, video_codec=v.get("codec_name"), size_bytes=int(size) if size else None)


def run(cmd: list[str], *, timeout: float = 900, log_path: Path | None = None) -> tuple[float, str]:
    """Run ffmpeg; return (seconds, stderr tail). Raises FFmpegError on a non-zero exit or timeout."""
    started = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"ffmpeg timed out after {timeout:.0f}s") from exc
    seconds = time.monotonic() - started
    tail = (proc.stderr or "")[-4000:]
    if log_path is not None:
        log_path.write_text(" ".join(cmd) + "\n\n" + (proc.stderr or ""), encoding="utf-8")
    if proc.returncode != 0:
        raise FFmpegError(f"ffmpeg exited with {proc.returncode}: {tail[-600:]}")
    return seconds, tail


def make_test_clip(path: Path, *, seconds: float = 12, width: int = 1280, height: int = 720, audio: bool = True, fps: int = 30) -> Path:
    """Synthetic source (testsrc2 + tone) for offline tests and demos. Never touches the network."""
    if FFMPEG is None:
        raise FFmpegError("ffmpeg not found on PATH")
    cmd = [FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate={fps}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
    cmd += ["-t", str(seconds), "-pix_fmt", "yuv420p", "-c:v", "libx264" if "libx264" in capabilities()["encoders"] else "mpeg4", "-preset", "ultrafast"]
    if audio:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd.append(str(path))
    subprocess.run(cmd, check=True, timeout=120)
    return path
