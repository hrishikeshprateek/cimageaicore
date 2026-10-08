"""Follow the speaker: a crop path instead of one fixed focus point.

A 16:9 talk in a 9:16 reel keeps about two thirds of the width, so a presenter who walks, or two people taking turns,
leaves the frame. `locate_subject()` answers "where do the faces sit on average"; this answers "where are they *now*",
sampled across the clip and smoothed into a handful of keyframes the renderer can drive the crop with.

Smoothing matters more than detection: a crop that chases every detection jitters and looks worse than a static one.
So the path gets an exponential smoother, a deadband (small movements are ignored), a speed limit, and is then reduced
to the fewest keyframes that still describe it.
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from services.video_composer import ffmpeg as ff
from services.video_composer.framing import FRAME_WIDTH, Face, detect_faces, detector_available
from services.video_composer.timeline import FocusKey

log = logging.getLogger(__name__)

SAMPLE_SECONDS = 0.5          # how often to look
MAX_SAMPLES = 120             # a minute of footage at 0.5s; longer clips sample coarser
SMOOTHING = 0.35              # exponential smoother: lower = calmer camera
DEADBAND = 0.035              # ignore movement smaller than this (fraction of the frame)
MAX_SPEED = 0.25              # fraction of the frame per second - a pan, never a snap
SIMPLIFY = 0.012              # drop a keyframe the straight line already explains this closely


def dominant_face(faces: list[Face], previous: tuple[float, float] | None) -> tuple[float, float] | None:
    """One point per frame: the biggest face, unless a smaller one is clearly the one we were already following."""
    if not faces:
        return None
    if previous is None:
        f = max(faces, key=lambda x: x.w * x.h)
        return f.cx, f.cy
    px, py = previous

    def cost(f: Face) -> float:
        d = ((f.cx - px) ** 2 + (f.cy - py) ** 2) ** 0.5
        return d - 1.5 * (f.w * f.h) ** 0.5        # near the last position, and big, wins
    f = min(faces, key=cost)
    return f.cx, f.cy


def smooth(points: list[tuple[float, float, float]], *, alpha: float = SMOOTHING, deadband: float = DEADBAND,
           max_speed: float = MAX_SPEED) -> list[tuple[float, float, float]]:
    """(t, x, y) samples -> a calm path: exponential smoothing, a deadband, then a speed limit."""
    out: list[tuple[float, float, float]] = []
    cx = cy = None
    for t, x, y in points:
        if cx is None:
            cx, cy = x, y
        else:
            tx = cx + alpha * (x - cx)
            ty = cy + alpha * (y - cy)
            if abs(tx - cx) < deadband:
                tx = cx
            if abs(ty - cy) < deadband:
                ty = cy
            dt = max(1e-3, t - out[-1][0])
            step = max_speed * dt
            cx += max(-step, min(step, tx - cx))
            cy += max(-step, min(step, ty - cy))
        out.append((round(t, 3), round(min(max(cx, 0.0), 1.0), 4), round(min(max(cy, 0.0), 1.0), 4)))
    return out


def simplify(points: list[tuple[float, float, float]], tolerance: float = SIMPLIFY) -> list[tuple[float, float, float]]:
    """Keep only the keyframes a straight line cannot replace (Douglas-Peucker on the x/y path)."""
    if len(points) <= 2:
        return points
    first, last = points[0], points[-1]
    span = max(1e-6, last[0] - first[0])

    def deviation(p):
        f = (p[0] - first[0]) / span
        return max(abs(p[1] - (first[1] + f * (last[1] - first[1]))), abs(p[2] - (first[2] + f * (last[2] - first[2]))))

    worst = max(points[1:-1], key=deviation)
    if deviation(worst) <= tolerance:
        return [first, last]
    i = points.index(worst)
    return simplify(points[: i + 1], tolerance)[:-1] + simplify(points[i:], tolerance)


def track_faces(video: Path, cut_in: float, cut_out: float, *, every: float = SAMPLE_SECONDS,
                max_samples: int = MAX_SAMPLES) -> list[FocusKey]:
    """Sample the clip and return the path the crop should follow (times relative to the clip start).

    Empty when there is no detector, nothing was found, or the subject never really moves - the caller then uses the
    static focus, which is both cheaper and steadier."""
    dur = max(0.0, cut_out - cut_in)
    if dur < 1.0 or not detector_available():
        return []
    step = max(every, dur / max_samples)
    times = [cut_in + i * step for i in range(int(dur // step) + 1) if cut_in + i * step < cut_out - 0.05]
    raw: list[tuple[float, float, float]] = []
    previous: tuple[float, float] | None = None
    with tempfile.TemporaryDirectory(prefix="track-") as tmp:
        for i, t in enumerate(times):
            frame = ff.poster_frame(video, Path(tmp) / f"t{i}.jpg", at=t, max_width=FRAME_WIDTH)
            if frame is None:
                continue
            try:
                faces = detect_faces(frame)
            except Exception as exc:  # noqa: BLE001 - one bad frame must not sink the track
                log.warning("face detection failed at %.2fs: %s", t, exc)
                continue
            point = dominant_face(faces, previous)
            if point is None:
                continue
            previous = point
            raw.append((round(t - cut_in, 3), point[0], point[1]))
    if len(raw) < 2:
        return []
    path = simplify(smooth(raw))
    moved = max(p[1] for p in path) - min(p[1] for p in path), max(p[2] for p in path) - min(p[2] for p in path)
    if max(moved) < DEADBAND * 1.5:          # the speaker stood still: a static focus is better than a nervous one
        return []
    return [FocusKey(t=t, x=x, y=y) for t, x, y in path]


def crop_expression(keys: list[FocusKey], *, scaled: int, crop: int, axis: str = "x", duration: float | None = None) -> str:
    """ffmpeg crop x/y as a piecewise-linear function of t, built from the keyframes.

    `scaled` is the size of the scaled source along this axis, `crop` what we keep of it. The result is clamped so the
    window never leaves the frame."""
    span = max(0, scaled - crop)
    if not keys or span == 0:
        centre = (keys[0].x if axis == "x" else keys[0].y) if keys else 0.5
        return str(int(round(min(max(centre * scaled - crop / 2, 0), span))))

    def pos(k: FocusKey) -> int:
        c = k.x if axis == "x" else k.y
        return int(round(min(max(c * scaled - crop / 2, 0), span)))

    pts = [(k.t, pos(k)) for k in keys]
    if duration:
        pts = [(t, v) for t, v in pts if t <= duration + 0.001] or pts[:1]
    if len(pts) == 1:
        return str(pts[0][1])
    expr = str(pts[-1][1])                                     # after the last key, hold it
    for (t0, v0), (t1, v1) in reversed(list(zip(pts, pts[1:]))):
        dt = max(1e-3, t1 - t0)
        ramp = f"({v0}+({v1 - v0})*(t-{t0:.3f})/{dt:.3f})" if v1 != v0 else str(v0)
        expr = f"if(lt(t,{t1:.3f}),{ramp},{expr})"
    return f"'{expr}'"
