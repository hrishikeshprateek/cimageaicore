"""Deterministic ffmpeg rendering: cut -> fit into the template -> layers -> captions -> lower-third -> export preset.

`build_command()` is a pure function of the spec and the already-rendered text PNGs, so it can be
unit-tested without running ffmpeg; `render()` executes it.
"""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from services.video_composer import ffmpeg as ff
from services.video_composer.captions import CaptionCue, write_sidecars
from services.video_composer.settings import ComposerSettings
from services.video_composer.template import PRESETS, CaptionStyle, ResolvedLayout, Template, resolve_layout
from services.video_composer.textrender import CaptionJob, FontPaths, LowerThirdJob, TextResult, render_jobs

log = logging.getLogger(__name__)


class LowerThird(BaseModel):
    name: str
    role: str | None = None
    start: float | None = None      # seconds into the cut; None = template default
    end: float | None = None


class RenderSpec(BaseModel):
    """Everything that determines the output. Persisted with the render record."""

    job_id: str
    source_path: str
    source_width: int
    source_height: int
    source_has_audio: bool
    cut_in: float = Field(ge=0)
    cut_out: float
    preset: Literal["reels", "square", "landscape"] = "reels"
    template: str = "placeholder"
    fit: Literal["contain", "cover"] | None = None
    captions: list[CaptionCue] = Field(default_factory=list)
    captions_enabled: bool = True
    lower_third: LowerThird | None = None
    title: str | None = None
    caption_engine: Literal["overlay", "libass"] = "overlay"
    fps: int = 30
    crf: int = 20
    x264_preset: str = "medium"
    audio_bitrate: str = "160k"
    audio_fade: float = 0.15

    @property
    def duration(self) -> float:
        return round(self.cut_out - self.cut_in, 3)


class RenderOutput(BaseModel):
    output_path: str
    captions_srt: str | None = None
    captions_ass: str | None = None
    width: int
    height: int
    duration: float
    size_bytes: int
    ffmpeg_command: list[str]
    render_seconds: float
    layout: ResolvedLayout
    text_shaping: bool
    log_tail: str = ""


class _Placed(BaseModel):
    """A rendered overlay PNG with its canvas position and optional time window."""

    path: str
    x: int
    y: int
    start: float | None = None
    end: float | None = None
    kind: str = "layer"
    loop_video: bool = False


def _f(t: float) -> str:
    return f"{t:.3f}"


def _ff_escape(path: str) -> str:
    """Escape a path for use inside a filter option value (subtitles=filename=...)."""
    return path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def prepare_overlays(spec: RenderSpec, template: Template, layout: ResolvedLayout, work_dir: Path, *, fonts: FontPaths,
                     fribidi_dir: Path | None = None) -> tuple[list[_Placed], list[TextResult], bool]:
    """Render caption + lower-third PNGs and compute every overlay's position. Returns (placed, text results, shaped)."""
    jobs: list[CaptionJob | LowerThirdJob] = []
    windows: list[tuple[float, float, str]] = []
    if spec.captions_enabled and spec.caption_engine == "overlay":
        for i, cue in enumerate(spec.captions):
            if cue.end <= 0 or cue.start >= spec.duration:
                continue
            jobs.append(CaptionJob(text=cue.text, width=layout.caption_rect.w, style=template.caption, font_size=layout.caption_font_size,
                                   fonts=fonts, out=str(work_dir / f"cap_{i:03d}.png")))
            windows.append((max(0.0, cue.start), min(spec.duration, cue.end), "caption"))
    lt = spec.lower_third
    if lt and lt.name.strip():
        st = template.lower_third
        jobs.append(LowerThirdJob(name=lt.name.strip(), role=(lt.role or "").strip() or None, style=st, scale=layout.lower_third_scale,
                                  fonts=fonts, out=str(work_dir / "lower_third.png")))
        windows.append((lt.start if lt.start is not None else st.start, min(spec.duration, lt.end if lt.end is not None else st.end), "lower_third"))
    results, shaped = render_jobs(jobs, fribidi_override=fribidi_dir)
    placed: list[_Placed] = []
    for res, (start, end, kind) in zip(results, windows):
        if kind == "caption":
            y = layout.caption_rect.y if layout.caption_anchor == "top" else layout.caption_rect.bottom - res.height
            placed.append(_Placed(path=res.out, x=layout.caption_rect.x, y=max(0, y), start=start, end=end, kind="caption"))
        else:
            placed.append(_Placed(path=res.out, x=layout.lower_third_x, y=max(0, layout.lower_third_bottom - res.height), start=start, end=end, kind="lower_third"))
    return placed, results, shaped


def layer_inputs(template: Template, preset: str) -> list[_Placed]:
    lay = template.layouts[preset]
    out: list[_Placed] = []
    for layer in sorted((l for l in lay.layers if l.enabled), key=lambda l: l.z):
        p = template.layer_path(layer)
        out.append(_Placed(path=str(p), x=layer.x, y=layer.y, kind="layer", loop_video=layer.loop and p.suffix.lower() in {".mov", ".webm"}))
    return out


def build_command(spec: RenderSpec, template: Template, layout: ResolvedLayout, layers: list[_Placed], texts: list[_Placed], output: Path, *,
                  ass_path: Path | None = None, fonts_dir: Path | None = None, threads: int = 0) -> list[str]:
    """Pure: the exact ffmpeg invocation for this render."""
    if ff.FFMPEG is None:
        raise ff.FFmpegError("ffmpeg not found on PATH")
    lay = template.layouts[spec.preset]
    W, H = layout.width, layout.height
    dur = spec.duration
    cmd: list[str] = [ff.FFMPEG, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
                      "-ss", _f(spec.cut_in), "-t", _f(dur), "-i", spec.source_path]
    inputs = 1
    idx_of: list[int] = []
    for pl in layers + texts:
        suffix = Path(pl.path).suffix.lower()
        if suffix == ".png":
            cmd += ["-loop", "1", "-framerate", str(spec.fps), "-i", pl.path]
        elif suffix == ".webm":
            cmd += (["-stream_loop", "-1"] if pl.loop_video else []) + ["-c:v", "libvpx-vp9", "-i", pl.path]     # native vp9 decoder drops alpha
        else:
            cmd += (["-stream_loop", "-1"] if pl.loop_video else []) + ["-i", pl.path]
        idx_of.append(inputs)
        inputs += 1

    v = layout.video_rect
    fit = spec.fit or lay.fit
    if fit == "cover":
        vid_chain = f"scale={v.w}:{v.h}:force_original_aspect_ratio=increase:flags=lanczos,crop={v.w}:{v.h}"
    else:
        vid_chain = f"scale={v.w}:{v.h}:flags=lanczos"
    graph = [
        f"color=c={lay.background}:s={W}x{H}:r={spec.fps}:d={_f(dur)},format=yuv444p[bg]",
        f"[0:v]{vid_chain},setsar=1,fps={spec.fps},format=yuv444p[vid]",
        f"[bg][vid]overlay=x={v.x}:y={v.y}:format=yuv444:shortest=1[c0]",
    ]
    cur = "c0"
    n = 1
    for pl, idx in zip(layers + texts, idx_of):
        en = f":enable='between(t,{_f(pl.start)},{_f(pl.end)})'" if pl.start is not None and pl.end is not None else ""
        graph.append(f"[{cur}][{idx}:v]overlay=x={pl.x}:y={pl.y}:format=yuv444:eof_action=repeat{en}[c{n}]")
        cur = f"c{n}"
        n += 1
    if spec.captions_enabled and spec.caption_engine == "libass" and ass_path is not None:
        fd = f":fontsdir='{_ff_escape(str(fonts_dir))}'" if fonts_dir else ""
        graph.append(f"[{cur}]subtitles=filename='{_ff_escape(str(ass_path))}'{fd}[c{n}]")
        cur = f"c{n}"
    graph.append(f"[{cur}]format=yuv420p[v]")
    if spec.source_has_audio:
        fade = spec.audio_fade
        graph.append(f"[0:a]afade=t=in:st=0:d={_f(fade)},afade=t=out:st={_f(max(0.0, dur - fade))}:d={_f(fade)}[a]")

    enc, enc_extra = ff.video_encoder()
    cmd += ["-filter_complex", ";".join(graph), "-map", "[v]"]
    if spec.source_has_audio:
        cmd += ["-map", "[a]", "-c:a", "aac", "-b:a", spec.audio_bitrate, "-ar", "48000", "-ac", "2"]
    else:
        cmd += ["-an"]
    cmd += ["-c:v", enc, *enc_extra]
    if enc == "libx264":
        cmd += ["-preset", spec.x264_preset, "-crf", str(spec.crf), "-profile:v", "high", "-level", "4.1"]
    cmd += ["-pix_fmt", "yuv420p", "-r", str(spec.fps), "-movflags", "+faststart", "-t", _f(dur)]
    if threads:
        cmd += ["-threads", str(threads)]
    cmd.append(str(output))
    return cmd


def render(spec: RenderSpec, template: Template, settings: ComposerSettings, output: Path, *, keep_work: bool | None = None) -> RenderOutput:
    """Render one preset. Deterministic given the spec, template files and fonts."""
    if spec.preset not in template.layouts:
        raise ValueError(f"template '{template.name}' has no layout for preset '{spec.preset}'")
    if spec.cut_out <= spec.cut_in:
        raise ValueError("cut_out must be after cut_in")
    missing = template.missing_files()
    if missing:
        raise FileNotFoundError(f"template '{template.name}' is missing layer files: {', '.join(missing)}")
    fonts = font_paths(template, settings)
    lay = template.layouts[spec.preset]
    cap_style: CaptionStyle = template.caption
    font_size = lay.caption_font_size or cap_style.font_size
    cap_line = fonts.line_height("bold" if cap_style.bold else "regular", font_size, cap_style.line_height)
    layout = resolve_layout(lay, spec.preset, spec.source_width, spec.source_height, fit=spec.fit, caption=cap_style, caption_line_px=cap_line, lower_third=template.lower_third)

    output.parent.mkdir(parents=True, exist_ok=True)
    work = output.with_name(output.stem + "_work")
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    started = time.monotonic()
    try:
        texts, results, shaped = prepare_overlays(spec, template, layout, work, fonts=fonts, fribidi_dir=settings.fribidi_lib_dir)
        layers = layer_inputs(template, spec.preset)
        srt = ass = None
        if spec.captions:
            srt, ass = write_sidecars(spec.captions, output.with_suffix(".srt"), width=layout.width, height=layout.height, font_size=font_size,
                                      margin_v=layout.height - layout.caption_rect.bottom, colour=cap_style.colour, box_colour=cap_style.box_colour,
                                      box_opacity=cap_style.box_opacity)
        cmd = build_command(spec, template, layout, layers, texts, output, ass_path=ass, fonts_dir=Path(fonts.regular).parent, threads=settings.ffmpeg_threads)
        seconds, tail = ff.run(cmd, timeout=settings.ffmpeg_timeout_seconds, log_path=work / "ffmpeg.log")
        info = ff.probe(output)
        return RenderOutput(
            output_path=str(output), captions_srt=str(srt) if srt else None, captions_ass=str(ass) if ass else None,
            width=info.width, height=info.height, duration=info.duration, size_bytes=info.size_bytes or output.stat().st_size,
            ffmpeg_command=cmd, render_seconds=round(time.monotonic() - started, 2), layout=layout, text_shaping=bool(shaped), log_tail=tail[-1500:],
        )
    finally:
        if not (settings.keep_work_files if keep_work is None else keep_work):
            shutil.rmtree(work, ignore_errors=True)


def font_paths(template: Template, settings: ComposerSettings) -> FontPaths:
    paths = FontPaths(
        regular=str(template.font_path("regular", settings.font_regular_path)), bold=str(template.font_path("bold", settings.font_bold_path)),
        fallback_regular=str(settings.font_fallback_regular_path), fallback_bold=str(settings.font_fallback_bold_path),
    )
    for f in (paths.regular, paths.bold, paths.fallback_regular, paths.fallback_bold):
        if f and not Path(f).exists():
            raise FileNotFoundError(f"font not found: {f}")
    return paths


def output_path_for(settings: ComposerSettings, job_id: str, render_id: str, preset: str) -> Path:
    return settings.renders_dir / job_id / f"{render_id}_{preset}.mp4"


def preset_size(preset: str) -> tuple[int, int]:
    return PRESETS[preset]
