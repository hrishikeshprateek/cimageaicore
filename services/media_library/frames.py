"""Pull still frames out of a video with ffmpeg, picking the sharpest of a few candidates around the timestamp."""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageFilter, ImageStat

log = logging.getLogger(__name__)
FFMPEG = shutil.which("ffmpeg")
MAX_WIDTH = 1600
CANDIDATE_OFFSETS = (-0.4, 0.0, 0.4)


class FrameError(RuntimeError):
    pass


def sharpness(path: Path) -> float:
    """Variance of the edge image - motion-blurred or fading frames score low."""
    with Image.open(path) as im:
        g = im.convert("L")
        g.thumbnail((640, 640))
        return float(ImageStat.Stat(g.filter(ImageFilter.FIND_EDGES)).var[0])


def _grab(video: Path, at: float, out: Path, max_width: int) -> bool:
    proc = subprocess.run(
        [FFMPEG, "-v", "error", "-y", "-ss", f"{max(0.0, at):.3f}", "-i", str(video), "-frames:v", "1",
         "-vf", f"scale='min({max_width},iw)':-2", "-q:v", "2", str(out)],
        capture_output=True, text=True, timeout=120, check=False,
    )
    return proc.returncode == 0 and out.exists() and out.stat().st_size > 0


def extract_frame(video: Path, at: float, out: Path, *, max_width: int = MAX_WIDTH, duration: float | None = None,
                  offsets: tuple[float, ...] = CANDIDATE_OFFSETS) -> tuple[Path, int, int]:
    """Write the sharpest frame near `at` seconds to `out` (JPEG). Returns (path, width, height)."""
    if FFMPEG is None:
        raise FrameError("ffmpeg not found on PATH")
    if not video.exists():
        raise FrameError(f"video not found: {video}")
    out.parent.mkdir(parents=True, exist_ok=True)
    best: tuple[float, Path] | None = None
    with tempfile.TemporaryDirectory() as tmp:
        for i, off in enumerate(offsets):
            t = at + off
            if t < 0 or (duration and t > duration - 0.05):
                continue
            cand = Path(tmp) / f"c{i}.jpg"
            if not _grab(video, t, cand, max_width):
                continue
            score = sharpness(cand)
            if best is None or score > best[0]:
                best = (score, cand)
        if best is None:   # timestamp beyond the end (analysis estimates drift) - take the last decodable frame instead
            cand = Path(tmp) / "last.jpg"
            if duration and _grab(video, max(0.0, duration - 0.5), cand, max_width):
                best = (0.0, cand)
        if best is None:
            raise FrameError(f"could not decode a frame near {at:.1f}s of {video.name}")
        shutil.copy(best[1], out)
    with Image.open(out) as im:
        w, h = im.size
    return out, w, h


def image_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as im:
        return im.size
