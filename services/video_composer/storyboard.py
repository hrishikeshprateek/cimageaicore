"""Script -> finished video, using the same reel machinery.

Each scene becomes one branded segment (the template frame, the on-screen text as a caption) rendered by `render()`:
 * the scene points at a video moment (b_roll_block_id -> job + timestamp) -> that window of the analysed video,
 * or at a picture (b_roll_image_id) -> the still with a slow push-in,
 * or at nothing -> a brand slate saying what has to be filmed, so the cut is still watchable end to end.
The segments are concatenated (stream copy - they share preset, template and encoder settings) and the voiceover is laid
over the result, each line inside its own scene so the voice stays in sync with the picture.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from services.video_composer import ffmpeg as ff
from services.video_composer.captions import CaptionCue
from services.video_composer.renderer import RenderSpec, render
from services.video_composer.settings import ComposerSettings
from services.video_composer.template import PRESETS, Template

log = logging.getLogger(__name__)
SegmentKind = Literal["clip", "still", "slate"]
MIN_SCENE_SECONDS = 1.0
MAX_TOTAL_SECONDS = 1800.0


@dataclass
class Segment:
    n: int
    seconds: float
    kind: SegmentKind
    text: str | None = None            # on-screen text, shown for the whole segment
    source_path: str | None = None     # clip: the analysed video · still: the image file
    cut_in: float = 0.0
    note: str = ""                     # why it looks like this - surfaced in the render detail
    voice_path: str | None = None


@dataclass
class Storyboard:
    segments: list[Segment]
    warnings: list[str] = field(default_factory=list)
    job_id: str | None = None          # the video most of the footage comes from (the render record hangs off it)

    @property
    def seconds(self) -> float:
        return round(sum(s.seconds for s in self.segments), 2)


def ts_seconds(ts: str | None) -> float | None:
    """'00:01:06' / '1:06' / '66.5' -> seconds."""
    if not ts:
        return None
    parts = str(ts).strip().split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    out = 0.0
    for p in nums:
        out = out * 60 + p
    return out


def plan(script, *, job_for: Callable[[str], object | None], image_path: Callable[[str], Path | None],
         voice_for: Callable[[int], Path | None] = lambda n: None) -> Storyboard:
    """Turn a stored script into segments. `job_for(job_id)` returns the analysis job, `image_path(image_id)` the file."""
    blocks = {b.get("block_id"): b for b in ((script.evidence or {}).get("blocks") or [])}
    segments: list[Segment] = []
    warnings: list[str] = []
    jobs_used: list[str] = []
    for sc in script.scenes or []:
        n = int(sc.get("n") or len(segments) + 1)
        seconds = max(MIN_SCENE_SECONDS, float(sc.get("seconds") or 0))
        text = (sc.get("on_screen_text") or "").strip() or None
        seg = Segment(n=n, seconds=seconds, kind="slate", text=text, voice_path=str(voice_for(n)) if voice_for(n) else None)
        bid = sc.get("b_roll_block_id")
        if bid:
            job_id = str(bid).split(":")[0]
            job = job_for(job_id)
            raw = getattr(getattr(job, "source", None), "path", None) if job else None
            path = Path(raw) if raw else None          # a YouTube-analysed job has no local file: Path("") would be "."
            at = ts_seconds((blocks.get(bid) or {}).get("timestamp"))
            if job is None or path is None or not path.exists():
                warnings.append(f"scene {n}: the video behind {bid} is not on this machine (analysed from a link?) - using a picture instead")
            elif at is None:
                warnings.append(f"scene {n}: {bid} has no timestamp - using a picture instead")
            else:
                dur = getattr(getattr(job, "source", None), "duration_seconds", None) or 0
                cut_in = max(0.0, min(at, max(0.0, dur - seconds))) if dur else max(0.0, at)
                seg.kind, seg.source_path, seg.cut_in = "clip", str(path), round(cut_in, 2)
                seg.note = f"{getattr(job.source, 'name', job_id)} @ {blocks.get(bid, {}).get('timestamp')}"
                jobs_used.append(job_id)
        if seg.kind != "clip" and sc.get("b_roll_image_id"):
            p = image_path(sc["b_roll_image_id"])
            if p and p.exists():
                seg.kind, seg.source_path, seg.note = "still", str(p), f"still {sc['b_roll_image_id']}"
            else:
                warnings.append(f"scene {n}: picture {sc['b_roll_image_id']} is missing")
        if seg.kind == "slate":
            seg.note = (sc.get("visual") or "to be filmed")[:120]
            warnings.append(f"scene {n}: no footage yet - a slate stands in ({seg.note[:60]})")
        segments.append(seg)
    if not segments:
        raise ValueError("this script has no scenes")
    total = sum(s.seconds for s in segments)
    if total > MAX_TOTAL_SECONDS:
        raise ValueError(f"the script is {total / 60:.0f} minutes long - render it in parts")
    job_id = max(set(jobs_used), key=jobs_used.count) if jobs_used else None
    return Storyboard(segments=segments, warnings=warnings, job_id=job_id)


# ------------------------------------------------------------------ ffmpeg pieces
def _still_clip(image: Path, out: Path, seconds: float, fps: int, *, zoom: float = 1.10) -> Path:
    """A still becomes a short clip with a slow push-in, so a picture scene does not look like a frozen frame."""
    info = subprocess.run([ff.FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(image)],
                          capture_output=True, text=True, check=False)
    try:
        w, h = (int(x) for x in (info.stdout.strip() or "1280x720").split("x")[:2])
    except ValueError:
        w, h = 1280, 720
    w, h = max(320, w - w % 2), max(320, h - h % 2)
    frames = max(2, int(seconds * fps))
    vf = (f"scale={w}:{h}:flags=lanczos,zoompan=z='min(1+on/{frames}*{zoom - 1:.4f},{zoom})':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
          f":d={frames}:s={w}x{h}:fps={fps},setsar=1")
    cmd = [ff.FFMPEG, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-loop", "1", "-i", str(image), "-t", f"{seconds:.3f}",
           "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-r", str(fps), str(out)]
    ff.run(cmd, timeout=300)
    return out


def _slate_clip(out: Path, seconds: float, fps: int, colour: str, size: tuple[int, int] = (1280, 720)) -> Path:
    cmd = [ff.FFMPEG, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-f", "lavfi",
           "-i", f"color=c={colour}:s={size[0]}x{size[1]}:r={fps}:d={seconds:.3f}",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-pix_fmt", "yuv420p", "-t", f"{seconds:.3f}", str(out)]
    ff.run(cmd, timeout=120)
    return out


def voice_track(segments: list[Segment], out: Path, *, sample_rate: int = 48000) -> Path | None:
    """One audio track where each scene's line sits inside that scene's window (padded or trimmed to fit)."""
    if not any(s.voice_path for s in segments):
        return None
    cmd = [ff.FFMPEG, "-hide_banner", "-nostdin", "-y", "-loglevel", "error"]
    parts, idx = [], 0
    for s in segments:
        if s.voice_path and Path(s.voice_path).exists():
            cmd += ["-i", s.voice_path]
            parts.append(f"[{idx}:a]aresample={sample_rate},apad,atrim=0:{s.seconds:.3f},asetpts=N/SR/TB[v{idx}]")
            idx += 1
        else:
            cmd += ["-f", "lavfi", "-t", f"{s.seconds:.3f}", "-i", f"anullsrc=r={sample_rate}:cl=stereo"]
            parts.append(f"[{idx}:a]atrim=0:{s.seconds:.3f},asetpts=N/SR/TB[v{idx}]")
            idx += 1
    graph = ";".join(parts) + ";" + "".join(f"[v{i}]" for i in range(idx)) + f"concat=n={idx}:v=0:a=1[out]"
    cmd += ["-filter_complex", graph, "-map", "[out]", "-c:a", "aac", "-b:a", "160k", "-ar", str(sample_rate), "-ac", "2", str(out)]
    ff.run(cmd, timeout=600)
    return out


def _concat(parts: list[Path], out: Path, work: Path) -> Path:
    listing = work / "concat.txt"
    # absolute, or ffmpeg resolves each entry relative to the list file and looks in the wrong place
    listing.write_text("".join("file '{}'\n".format(p.resolve().as_posix().replace("'", "'\\''")) for p in parts), encoding="utf-8")
    ff.run([ff.FFMPEG, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
            "-c", "copy", "-movflags", "+faststart", str(out)], timeout=900)
    return out


def _mux(video: Path, voice: Path | None, out: Path, *, audio: str, source_volume: float = 0.15) -> Path:
    """audio: 'voiceover' (voice only) · 'both' (voice over ducked source) · 'source' (leave the video's own sound)."""
    if voice is None or audio == "source":
        shutil.move(str(video), str(out))
        return out
    cmd = [ff.FFMPEG, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-i", str(video), "-i", str(voice)]
    if audio == "both":
        graph = f"[0:a]volume={source_volume}[s];[1:a]apad[v];[s][v]amix=inputs=2:duration=first:normalize=0[a]"
    else:
        graph = "[1:a]apad[a]"
    cmd += ["-filter_complex", graph, "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-shortest", "-movflags", "+faststart", str(out)]
    ff.run(cmd, timeout=900)
    return out


def render_storyboard(board: Storyboard, template: Template, settings: ComposerSettings, output: Path, *,
                      preset: str = "reels", audio: str = "voiceover", fit: str | None = None,
                      on_progress: Callable[[int, int], None] | None = None) -> dict:
    """Render every scene, stitch them and lay the voiceover over the result. Returns detail for the render record."""
    if preset not in PRESETS:
        raise ValueError(f"unknown preset {preset}")
    if ff.FFMPEG is None or ff.FFPROBE is None:
        raise ff.FFmpegError("ffmpeg/ffprobe not found on PATH")
    started = time.monotonic()
    output.parent.mkdir(parents=True, exist_ok=True)
    work = output.with_name(output.stem + "_scenes")
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    fps = 30
    try:
        parts: list[Path] = []
        details: list[dict] = []
        for i, seg in enumerate(board.segments):
            if on_progress:
                on_progress(i, len(board.segments))
            if seg.kind == "clip" and not (seg.source_path and Path(seg.source_path).is_file()):
                seg.kind, seg.source_path = "slate", None          # belt and braces: never hand ffprobe a missing file
                board.warnings.append(f"scene {seg.n}: the clip is gone - a slate stands in")
            if seg.kind == "clip":
                src = Path(seg.source_path)
                info = ff.probe(src)
                cut_in = max(0.0, min(seg.cut_in, max(0.0, info.duration - seg.seconds)))
                spec = RenderSpec(job_id=output.stem, source_path=str(src), source_width=info.width, source_height=info.height,
                                  source_has_audio=info.has_audio, cut_in=cut_in, cut_out=cut_in + seg.seconds)
            else:
                base = work / f"base_{seg.n:03d}.mp4"
                if seg.kind == "still" and seg.source_path and Path(seg.source_path).is_file():
                    _still_clip(Path(seg.source_path), base, seg.seconds, fps)
                else:
                    _slate_clip(base, seg.seconds, fps, template.layouts[preset].background)
                info = ff.probe(base)
                spec = RenderSpec(job_id=output.stem, source_path=str(base), source_width=info.width, source_height=info.height,
                                  source_has_audio=False, cut_in=0.0, cut_out=seg.seconds)
            spec.preset = preset
            spec.template = template.name
            spec.fit = fit
            spec.fps = fps
            spec.force_audio_track = True                       # every segment carries audio, or concat cannot stream-copy
            spec.captions = [CaptionCue(start=0.0, end=seg.seconds, text=seg.text)] if seg.text else []
            out_i = work / f"scene_{seg.n:03d}.mp4"
            render(spec, template, settings, out_i, keep_work=False)
            parts.append(out_i)
            details.append({"scene": seg.n, "kind": seg.kind, "seconds": seg.seconds, "source": seg.note, "text": seg.text})
        stitched = _concat(parts, work / "stitched.mp4", work)
        voice = voice_track(board.segments, work / "voice.m4a") if audio != "source" else None
        _mux(stitched, voice, output, audio=audio)
        info = ff.probe(output)
        poster = ff.poster_frame(output, output.with_suffix(".jpg"), at=min(1.0, info.duration / 2))
        return {
            "kind": "storyboard", "scenes": details, "warnings": board.warnings, "audio": audio, "voice": bool(voice),
            "width": info.width, "height": info.height, "duration": info.duration, "size_bytes": info.size_bytes or output.stat().st_size,
            "poster": str(poster) if poster else None, "render_seconds": round(time.monotonic() - started, 2), "output_path": str(output),
        }
    finally:
        if not settings.keep_work_files:
            shutil.rmtree(work, ignore_errors=True)
