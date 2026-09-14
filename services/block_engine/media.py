"""Small media helpers (ffprobe, timestamps, hashing)."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

FFPROBE = shutil.which("ffprobe")


def ffprobe_available() -> bool:
    return FFPROBE is not None


def probe_duration_seconds(path: Path | None) -> float | None:
    if path is None or FFPROBE is None or not path.exists():
        return None
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30, check=False,
        )
        data = json.loads(out.stdout or "{}")
        dur = data.get("format", {}).get("duration")
        return float(dur) if dur else None
    except (subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return None


def seconds_to_ts(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def ts_to_seconds(ts: str) -> float | None:
    try:
        parts = [int(p) for p in ts.strip().split(":")]
    except ValueError:
        return None
    if len(parts) == 3:
        h, m, s = parts
    elif len(parts) == 2:
        h, (m, s) = 0, parts
    else:
        return None
    return h * 3600 + m * 60 + s


def sha256_of(path: Path, chunk: int = 4 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()
