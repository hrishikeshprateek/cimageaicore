"""Upload proxies: shrink raw camera files before they go to Gemini.

Why: the model's cost depends on *duration* (frames sampled per second + audio tokens), not on bytes - a 21 GB ProRes
master and a 200 MB H.264 of the same ten minutes cost the same. Bytes only hurt: the Gemini File API refuses files
over 2 GB, and a 21 GB upload takes a quarter of an hour on a 200 Mbps line. Gemini samples video at ~1 fps and
downsizes frames anyway, so a 720p / CRF 28 proxy loses nothing the model would have seen.

The original stays where it is (the composer cuts reels from it); the proxy lives in data/proxies/ and is deleted
after the analysis unless PROXY_KEEP=true.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
GEMINI_MAX_UPLOAD_BYTES = 2 * 1024 ** 3        # File API hard limit


class ProxyError(RuntimeError):
    pass


@dataclass
class MediaProbe:
    width: int
    height: int
    duration: float
    size_bytes: int
    bitrate_kbps: int
    video_codec: str | None
    fps: float | None


@dataclass
class ProxyPolicy:
    enabled: bool = True
    min_mb: int = 400                  # sources at or above this size get a proxy
    max_height: int = 720
    max_bitrate_kbps: int = 6000       # sources above this bitrate get a proxy even if small
    crf: int = 28
    preset: str = "veryfast"
    audio_kbps: int = 96
    keep: bool = False
    timeout_seconds: int = 7200


def probe(path: Path) -> MediaProbe:
    if FFPROBE is None:
        raise ProxyError("ffprobe not found on PATH")
    proc = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,codec_name,avg_frame_rate,side_data_list:stream_tags=rotate:format=duration,size,bit_rate",
                           "-of", "json", str(path)], capture_output=True, text=True, timeout=120, check=False)
    if proc.returncode != 0:
        raise ProxyError(f"ffprobe failed for {path.name}: {proc.stderr.strip()[-300:]}")
    data = json.loads(proc.stdout or "{}")
    v = (data.get("streams") or [{}])[0]
    fmt = data.get("format", {})
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    rotation = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            try:
                rotation = int(round(float(sd["rotation"])))
            except (TypeError, ValueError):
                pass
    if abs(rotation) % 180 == 90:
        w, h = h, w
    fps = None
    raw = v.get("avg_frame_rate") or ""
    if "/" in raw:
        num, den = raw.split("/")
        if float(den or 0):
            fps = float(num) / float(den)
    size = int(fmt.get("size") or path.stat().st_size)
    dur = float(fmt.get("duration") or 0)
    br = int(fmt.get("bit_rate") or 0) // 1000 or (int(size * 8 / dur / 1000) if dur else 0)
    return MediaProbe(width=w, height=h, duration=dur, size_bytes=size, bitrate_kbps=br, video_codec=v.get("codec_name"), fps=fps)


def proxy_reason(info: MediaProbe, policy: ProxyPolicy) -> str | None:
    """Why this source should be shrunk before upload, or None to send it as-is."""
    if info.size_bytes >= GEMINI_MAX_UPLOAD_BYTES:
        return f"{info.size_bytes / 1024 ** 3:.1f} GB exceeds the 2 GB upload limit"
    if not policy.enabled:
        return None
    if info.size_bytes >= policy.min_mb * 1024 ** 2:
        return f"{info.size_bytes / 1024 ** 2:.0f} MB ≥ {policy.min_mb} MB"
    if info.bitrate_kbps > policy.max_bitrate_kbps:
        return f"{info.bitrate_kbps / 1000:.1f} Mbps > {policy.max_bitrate_kbps / 1000:.0f} Mbps"
    if info.height > policy.max_height and info.size_bytes >= policy.min_mb * 1024 ** 2 // 4:
        return f"{info.height}p > {policy.max_height}p"
    return None


def make_proxy(src: Path, out: Path, policy: ProxyPolicy, *, info: MediaProbe | None = None) -> Path:
    """Transcode `src` to a small H.264/AAC MP4 at `out` (max `policy.max_height` tall, CRF `policy.crf`)."""
    if FFMPEG is None:
        raise ProxyError("ffmpeg not found on PATH")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part.mp4")
    scale = f"scale=-2:'min({policy.max_height},ih)'"
    cmd = [FFMPEG, "-v", "error", "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a?", "-vf", scale,
           "-c:v", "libx264", "-preset", policy.preset, "-crf", str(policy.crf), "-pix_fmt", "yuv420p", "-profile:v", "high",
           "-c:a", "aac", "-b:a", f"{policy.audio_kbps}k", "-ac", "2", "-movflags", "+faststart", str(tmp)]
    started = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=policy.timeout_seconds, check=False)
    if proc.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        raise ProxyError(f"ffmpeg proxy failed for {src.name}: {proc.stderr.strip()[-400:]}")
    tmp.replace(out)
    log.info("proxy %s -> %s (%.0f MB -> %.0f MB) in %.0fs", src.name, out.name, (info.size_bytes if info else src.stat().st_size) / 1e6, out.stat().st_size / 1e6, time.monotonic() - started)
    return out
