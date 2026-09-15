"""Video Composer API + page. Mounted by apps/api/main.py; every endpoint except /composer/system is gated by COMPOSER_ENABLED."""
from __future__ import annotations

import io
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, RedirectResponse, Response
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field

from apps.api.config import REPO_ROOT, get_settings
from apps.api.jobs import Job
from services.video_composer import ffmpeg as ff
from services.video_composer.captions import CaptionCue, cues_for_window
from services.video_composer.cuts import CutLimits, CutProposal, propose_cuts, refine_with_ai
from services.video_composer.framing import Subject, detector_available, focus_for_center, locate_subject
from services.video_composer.renderer import LowerThird, RenderSpec, output_path_for, render
from services.video_composer.settings import ComposerSettings, get_composer_settings
from services.video_composer.store import JsonRenderStore, PostgresRenderStore, RenderRecord, RenderWorker
from services.video_composer.template import (LAYER_EXTENSIONS, PRESET_LABELS, PRESETS, CoverCrop, Layer, Rect, Template, cover_crop, effective_fit, fit_rect,
                                              list_templates, load_template, save_template, template_path)
from services.video_composer.textrender import shaping_status

log = logging.getLogger(__name__)
router = APIRouter()
WEB_DIR = REPO_ROOT / "web"
MAX_RENDER_SECONDS = 300.0


# --------------------------------------------------------------------------------------------
# context (lazy: built on first use, lives on app.state.composer)
# --------------------------------------------------------------------------------------------

class ComposerContext:
    def __init__(self, app):
        self.app = app
        self.settings: ComposerSettings = get_composer_settings()
        self.settings.ensure_dirs()
        store = app.state.store
        if getattr(store, "kind", "") == "postgres":
            self.renders = PostgresRenderStore(store.pool)
        else:
            self.renders = JsonRenderStore(self.settings.renders_dir / "records")
        stale = self.renders.mark_stale()
        if stale:
            log.warning("marked %d interrupted render(s) as FAILED", stale)
        self.worker = RenderWorker(self.renders, self._run, self.settings.worker_threads)
        log.info("composer ready: template=%s renders=%s store=%s", self.settings.template, self.settings.renders_dir, self.renders.kind)

    def template(self, name: str | None = None) -> Template:
        return load_template(name or self.settings.template, self.settings.templates_dir)

    def limits(self) -> CutLimits:
        s = self.settings
        return CutLimits(min_seconds=s.min_cut_seconds, max_seconds=s.max_cut_seconds, target_seconds=s.target_cut_seconds, max_cuts=s.max_cuts,
                         max_chars_per_line=s.caption_max_chars_per_line, max_lines=s.caption_max_lines, caption_min_seconds=s.caption_min_seconds)

    def _run(self, rec: RenderRecord) -> None:
        template = self.template(rec.template)
        out = render(rec.spec, template, self.settings, output_path_for(self.settings, rec.job_id, rec.id, rec.preset))
        self.renders.update(
            rec.id, status="DONE", output_path=out.output_path, captions_path=out.captions_srt, width=out.width, height=out.height,
            duration_seconds=out.duration, size_bytes=out.size_bytes, ffmpeg_command=" ".join(out.ffmpeg_command), render_seconds=out.render_seconds,
            detail={"layout": out.layout.model_dump(), "text_shaping": out.text_shaping, "captions_ass": out.captions_ass, "poster": out.poster_path, "log_tail": out.log_tail},
        )
        log.info("render %s done: %s (%.1fs)", rec.id, out.output_path, out.render_seconds)


def _ctx(request: Request) -> ComposerContext:
    ctx = getattr(request.app.state, "composer", None)
    if ctx is None:
        ctx = ComposerContext(request.app)
        request.app.state.composer = ctx
    return ctx


def _enabled(request: Request) -> ComposerContext:
    if not get_composer_settings().enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "video composer is disabled - set COMPOSER_ENABLED=true in .env")
    return _ctx(request)


Ctx = Annotated[ComposerContext, Depends(_enabled)]


# --------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------

def _job(request: Request, job_id: str) -> Job:
    job = request.app.state.store.get(job_id)
    if job is None:
        raise HTTPException(404, f"job {job_id} not found")
    return job


def _local_media(job: Job) -> Path:
    if not job.source.path:
        raise HTTPException(400, f"job {job.id} has no local media (online sources cannot be composed - re-analyse from a file)")
    p = Path(job.source.path)
    if not p.exists():
        raise HTTPException(410, f"source file for job {job.id} is gone: {p}")
    return p


def _analysis(request: Request, job: Job):
    result = request.app.state.store.load_result(job)
    return result.analysis if result else None


# --------------------------------------------------------------------------------------------
# page + system
# --------------------------------------------------------------------------------------------

@router.get("/composer", include_in_schema=False)
def composer_page() -> RedirectResponse:
    """The studio now lives inside the admin UI."""
    return RedirectResponse("/admin#composer", status_code=302)


@router.get("/api/v1/composer/system")
def composer_system(request: Request) -> dict:
    s = get_composer_settings()
    info = {
        "enabled": s.enabled,
        "template": s.template,
        "templates_dir": str(s.templates_dir),
        "renders_dir": str(s.renders_dir),
        "presets": [{"id": k, "label": PRESET_LABELS[k], "width": w, "height": h} for k, (w, h) in PRESETS.items()],
        "ffmpeg": ff.capabilities(),
        "caption_engine": s.caption_engine,
        "text_shaping": shaping_status(s.fribidi_lib_dir),
        "fonts": {"regular": str(s.font_regular_path), "bold": str(s.font_bold_path), "fallback_regular": str(s.font_fallback_regular_path)},
        "cuts": {"min": s.min_cut_seconds, "max": s.max_cut_seconds, "target": s.target_cut_seconds, "max_cuts": s.max_cuts, "refine": s.cuts_refine},
    }
    if s.enabled:
        ctx = _ctx(request)
        info["render_store"] = ctx.renders.kind
        info["provider"] = getattr(request.app.state.engine.provider, "name", None)
        info["templates"] = list_templates(s.templates_dir)
    return info


# --------------------------------------------------------------------------------------------
# templates
# --------------------------------------------------------------------------------------------

@router.get("/api/v1/composer/templates")
def templates(ctx: Ctx) -> list[dict]:
    return list_templates(ctx.settings.templates_dir)


@router.get("/api/v1/composer/templates/{name}")
def get_template(ctx: Ctx, name: str) -> Template:
    try:
        return ctx.template(name)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(404, str(exc)) from exc


@router.put("/api/v1/composer/templates/{name}")
def put_template(ctx: Ctx, name: str, body: Template) -> Template:
    """Replace template.json (layouts, styles, brand). Layer files are managed with the /layers endpoints."""
    if name == "placeholder":
        raise HTTPException(400, "the placeholder is generated; copy it to a new name first (upload a layer to create one)")
    body.name = name
    save_template(body, ctx.settings.templates_dir)
    return ctx.template(name)


@router.get("/api/v1/composer/templates/{name}/preview")
def template_preview_png(ctx: Ctx, name: str, preset: str = "reels", aspect: str = "16:9") -> Response:
    from services.video_composer.preview import template_preview

    if preset not in PRESETS:
        raise HTTPException(400, f"unknown preset {preset}")
    try:
        t = ctx.template(name)
        a, b = (int(x) for x in aspect.split(":"))
        img = template_preview(t, preset, ctx.settings, source_aspect=(a, b))
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(404, str(exc)) from exc
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG")
    return Response(buf.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store"})


@router.post("/api/v1/composer/templates/{name}/layers", status_code=201)
async def upload_layer(
    ctx: Ctx, name: str,
    file: Annotated[UploadFile, File()],
    preset: Annotated[str, Form()] = "reels",
    layer: Annotated[str, Form()] = "frame",
    x: Annotated[int, Form()] = 0,
    y: Annotated[int, Form()] = 0,
    z: Annotated[int, Form()] = 0,
    replace_all: Annotated[bool, Form()] = False,
) -> Template:
    """Drop a real asset in: a PNG with alpha (or .mov ProRes 4444 / .webm alpha). Creates the template from the placeholder on first upload."""
    if preset not in PRESETS:
        raise HTTPException(400, f"unknown preset {preset}")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in LAYER_EXTENSIONS:
        raise HTTPException(400, f"layer must be one of {sorted(LAYER_EXTENSIONS)}")
    try:
        tpath = template_path(ctx.settings.templates_dir, name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if name == "placeholder":
        raise HTTPException(400, "upload to a new template name (e.g. 'cimage'); the placeholder is regenerated")
    if tpath.exists():
        t = ctx.template(name)
    else:   # start from the placeholder: same geometry, its PNGs copied so every preset still renders
        base = ctx.template("placeholder")
        tpath.parent.mkdir(parents=True, exist_ok=True)
        for lay in base.layouts.values():
            for l in lay.layers:
                shutil.copy(base.layer_path(l), tpath.parent / l.file)
        t = base.model_copy(update={"name": name, "description": f"{name} template (created from the placeholder geometry)"})
        t.dir = tpath.parent
    safe_layer = "".join(ch for ch in layer if ch.isalnum() or ch in "-_") or "layer"
    dest = tpath.parent / f"{preset}_{safe_layer}{ext}"
    with dest.open("wb") as out:
        while chunk := await file.read(8 * 1024 * 1024):
            out.write(chunk)
    lay = t.layouts[preset]
    layers = [] if replace_all else [l for l in lay.layers if l.name != safe_layer]
    layers.append(Layer(name=safe_layer, file=dest.name, x=x, y=y, z=z))
    lay.layers = sorted(layers, key=lambda l: l.z)
    save_template(t, ctx.settings.templates_dir)
    return ctx.template(name)


@router.delete("/api/v1/composer/templates/{name}/layers")
def delete_layer(ctx: Ctx, name: str, preset: str, layer: str) -> Template:
    t = ctx.template(name)
    if name == "placeholder":
        raise HTTPException(400, "the placeholder cannot be edited")
    lay = t.layouts.get(preset)
    if lay is None:
        raise HTTPException(404, f"no layout {preset}")
    lay.layers = [l for l in lay.layers if l.name != layer]
    save_template(t, ctx.settings.templates_dir)
    return ctx.template(name)


# --------------------------------------------------------------------------------------------
# cuts / captions / media
# --------------------------------------------------------------------------------------------

class CutsResponse(BaseModel):
    job_id: str
    duration_seconds: float | None
    refined: bool
    warning: str | None = None
    cuts: list[CutProposal]


@router.get("/api/v1/jobs/{job_id}/cuts")
def job_cuts(request: Request, ctx: Ctx, job_id: str, refine: bool | None = None) -> CutsResponse:
    """Proposed cuts for a job: rule-based, optionally refined by the AI Gateway (refine=true or COMPOSER_CUTS_REFINE=auto)."""
    job = _job(request, job_id)
    analysis = _analysis(request, job)
    if analysis is None:
        raise HTTPException(409, f"job {job_id} has no analysis yet (state={job.state})")
    duration = job.source.duration_seconds
    if duration is None and job.source.path and Path(job.source.path).exists():
        try:
            duration = ff.probe(Path(job.source.path)).duration
        except ff.FFmpegError:
            duration = None
    cuts = propose_cuts(analysis, duration, ctx.limits())
    warning = None
    refined = False
    want = ctx.settings.cuts_refine == "auto" if refine is None else refine
    if want:
        provider = request.app.state.engine.provider
        cuts, warning = refine_with_ai(provider, analysis, duration, cuts, lim=ctx.limits(), prompt_version=ctx.settings.cuts_prompt_version,
                                       institution_context=get_settings().institution_context)
        refined = warning is None
    return CutsResponse(job_id=job_id, duration_seconds=duration, refined=refined, warning=warning, cuts=cuts)


@router.get("/api/v1/jobs/{job_id}/captions")
def job_captions(request: Request, ctx: Ctx, job_id: str, cut_in: float, cut_out: float) -> list[CaptionCue]:
    """Caption cues (relative to cut_in) regenerated for a nudged window."""
    job = _job(request, job_id)
    analysis = _analysis(request, job)
    if analysis is None:
        return []
    if cut_out <= cut_in:
        raise HTTPException(400, "cut_out must be after cut_in")
    lim = ctx.limits()
    return cues_for_window(analysis.transcript, cut_in, cut_out, max_chars_per_line=lim.max_chars_per_line, max_lines=lim.max_lines, min_seconds=lim.caption_min_seconds)


@router.get("/api/v1/jobs/{job_id}/media")
def job_media(request: Request, ctx: Ctx, job_id: str) -> FileResponse:
    """Stream the job's source file for trimming previews (browser support depends on the container/codec)."""
    job = _job(request, job_id)
    p = _local_media(job)
    return FileResponse(p, media_type=job.source.mime_type or "video/mp4", filename=p.name)


# --------------------------------------------------------------------------------------------
# framing: how the source lands in the clip window, and where a crop should look
# --------------------------------------------------------------------------------------------

class FramingResponse(BaseModel):
    preset: str
    fit: Literal["contain", "cover"]
    source_width: int
    source_height: int
    video_rect: Rect                 # where the clip sits on the canvas
    crop: CoverCrop | None = None    # cover only: scale-to and crop window in scaled-source pixels
    focus_x: float = 0.5             # suggested focus (what the UI slider starts at)
    focus_y: float = 0.5
    subject: Subject                 # what face detection saw
    detector: bool                   # False -> no OpenCV/model on this box; suggestion is the centre


@router.get("/api/v1/jobs/{job_id}/framing")
def framing(request: Request, ctx: Ctx, job_id: str, cut_in: float = 0.0, cut_out: float | None = None,
            preset: Literal["reels", "square", "landscape"] = "reels", template: str | None = None,
            fit: Literal["contain", "cover", "auto"] | None = None, detect: bool = True) -> FramingResponse:
    """Resolve fit for this source/preset and, for a cover-crop, suggest a focus that keeps the people in frame."""
    job = _job(request, job_id)
    src = _local_media(job)
    try:
        info = ff.probe(src)
        tpl = ctx.template(template)
    except (ff.FFmpegError, FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if preset not in tpl.layouts:
        raise HTTPException(400, f"template '{tpl.name}' has no layout for preset '{preset}'")
    lay = tpl.layouts[preset]
    fit_eff = effective_fit(fit or lay.fit, info.width, info.height, lay.video_zone)
    rect = fit_rect(info.width, info.height, lay.video_zone, fit_eff, lay.video_align)
    subject = Subject(method="center")
    focus_x = focus_y = 0.5
    crop = None
    if fit_eff == "cover":
        crop = cover_crop(info.width, info.height, rect)
        if detect and detector_available():
            end = min(cut_out if cut_out is not None else info.duration, info.duration) if info.duration else (cut_out or cut_in + 1)
            subject = locate_subject(src, cut_in, max(end, cut_in + 0.1))
            if subject.center_x is not None:
                focus_x = focus_for_center(subject.center_x, rect.w / crop.scaled_w)
                focus_y = focus_for_center(subject.center_y, rect.h / crop.scaled_h)
        crop = cover_crop(info.width, info.height, rect, focus_x, focus_y)
    return FramingResponse(preset=preset, fit=fit_eff, source_width=info.width, source_height=info.height, video_rect=rect, crop=crop,
                           focus_x=focus_x, focus_y=focus_y, subject=subject, detector=detector_available())


@router.get("/api/v1/jobs/{job_id}/frame")
def frame(request: Request, ctx: Ctx, job_id: str, at: float = 0.0, width: int = 640):
    """One JPEG frame of the source at `at` seconds (for the crop preview in the composer)."""
    job = _job(request, job_id)
    src = _local_media(job)
    try:
        dur = ff.probe(src).duration
    except ff.FFmpegError as exc:
        raise HTTPException(400, f"cannot probe source: {exc}") from exc
    if dur:
        at = min(at, max(0.0, dur - 0.1))   # a seek past the end yields no frame
    fd, tmp = tempfile.mkstemp(prefix="frame-", suffix=".jpg")
    os.close(fd)
    out = ff.poster_frame(src, Path(tmp), at=max(0.0, at), max_width=min(max(width, 160), 1920))
    if out is None:
        Path(tmp).unlink(missing_ok=True)
        raise HTTPException(500, "could not extract a frame")
    return FileResponse(out, media_type="image/jpeg", background=BackgroundTask(lambda: Path(tmp).unlink(missing_ok=True)),
                        headers={"Cache-Control": "private, max-age=300"})


# --------------------------------------------------------------------------------------------
# compose / renders
# --------------------------------------------------------------------------------------------

class ComposeRequest(BaseModel):
    cut_in: float = Field(ge=0)
    cut_out: float
    presets: list[Literal["reels", "square", "landscape"]] = Field(default_factory=lambda: ["reels"])
    template: str | None = None
    captions: list[CaptionCue] | None = None       # None -> generated from the transcript for this window
    captions_enabled: bool = True
    lower_third: LowerThird | None = None
    fit: Literal["contain", "cover", "auto"] | None = None    # None/auto -> fill the clip window unless the crop would discard too much
    focus_x: float = Field(0.5, ge=0, le=1)                    # which part of a wide source survives the crop (0 left .. 1 right)
    focus_y: float = Field(0.5, ge=0, le=1)
    title: str | None = None
    cut_id: str | None = None


class ComposeResponse(BaseModel):
    job_id: str
    renders: list[RenderRecord]


@router.post("/api/v1/jobs/{job_id}/compose", status_code=status.HTTP_202_ACCEPTED)
def compose(request: Request, ctx: Ctx, job_id: str, body: ComposeRequest) -> ComposeResponse:
    job = _job(request, job_id)
    src = _local_media(job)
    if body.cut_out <= body.cut_in:
        raise HTTPException(400, "cut_out must be after cut_in")
    if body.cut_out - body.cut_in > MAX_RENDER_SECONDS:
        raise HTTPException(400, f"a cut may be at most {MAX_RENDER_SECONDS:.0f}s long")
    if not body.presets:
        raise HTTPException(400, "choose at least one preset")
    try:
        info = ff.probe(src)
    except ff.FFmpegError as exc:
        raise HTTPException(400, f"cannot probe source: {exc}") from exc
    if info.duration and body.cut_in >= info.duration:
        raise HTTPException(400, f"cut_in is beyond the end of the video ({info.duration:.1f}s)")
    cut_out = min(body.cut_out, info.duration) if info.duration else body.cut_out
    try:
        template = ctx.template(body.template)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    missing = [m for preset in body.presets for m in template.missing_files(preset)]
    if missing:
        raise HTTPException(400, f"template '{template.name}' is missing layer files: {', '.join(missing)}")

    captions = body.captions
    if captions is None and body.captions_enabled:
        analysis = _analysis(request, job)
        lim = ctx.limits()
        captions = cues_for_window(analysis.transcript, body.cut_in, cut_out, max_chars_per_line=lim.max_chars_per_line, max_lines=lim.max_lines,
                                   min_seconds=lim.caption_min_seconds) if analysis else []
    s = ctx.settings
    renders: list[RenderRecord] = []
    for preset in dict.fromkeys(body.presets):
        if preset not in template.layouts:
            raise HTTPException(400, f"template '{template.name}' has no layout for preset '{preset}'")
        spec = RenderSpec(
            job_id=job.id, source_path=str(src), source_width=info.width, source_height=info.height, source_has_audio=info.has_audio,
            cut_in=round(body.cut_in, 3), cut_out=round(cut_out, 3), preset=preset, template=template.name, fit=body.fit,
            focus_x=body.focus_x, focus_y=body.focus_y,
            captions=captions or [], captions_enabled=body.captions_enabled, lower_third=body.lower_third, title=body.title,
            caption_engine=s.caption_engine, fps=s.fps, crf=s.crf, x264_preset=s.x264_preset, audio_bitrate=s.audio_bitrate,
        )
        rec = RenderRecord(job_id=job.id, media_id=job.media_id, preset=preset, template=template.name, cut_in=spec.cut_in, cut_out=spec.cut_out,
                           title=body.title, cut_id=body.cut_id, spec=spec)
        ctx.renders.create(rec)
        ctx.worker.submit(rec.id)
        renders.append(rec)
    request.app.state.store.audit("composer", "compose.requested", "job", job.id, {"renders": [r.id for r in renders], "cut": [spec.cut_in, spec.cut_out], "presets": body.presets})
    return ComposeResponse(job_id=job.id, renders=renders)


@router.get("/api/v1/renders")
def list_renders(ctx: Ctx, job_id: str | None = None, limit: int = 100) -> list[RenderRecord]:
    return ctx.renders.list(job_id, min(max(limit, 1), 500))


@router.get("/api/v1/renders/{render_id}")
def get_render(ctx: Ctx, render_id: str) -> RenderRecord:
    rec = ctx.renders.get(render_id)
    if rec is None:
        raise HTTPException(404, f"render {render_id} not found")
    return rec


@router.get("/api/v1/renders/{render_id}/video")
def render_video(ctx: Ctx, render_id: str, download: bool = False) -> FileResponse:
    rec = get_render(ctx, render_id)
    if rec.status != "DONE" or not rec.output_path or not Path(rec.output_path).exists():
        raise HTTPException(409, f"render {render_id} is {rec.status}")
    p = Path(rec.output_path)
    return FileResponse(p, media_type="video/mp4", filename=p.name, content_disposition_type="attachment" if download else "inline")


@router.get("/api/v1/renders/{render_id}/poster.jpg")
def render_poster(ctx: Ctx, render_id: str) -> FileResponse:
    rec = get_render(ctx, render_id)
    poster = rec.detail.get("poster")
    if not poster or not Path(poster).exists():
        raise HTTPException(404, "no poster for this render")
    return FileResponse(Path(poster), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@router.get("/api/v1/renders/{render_id}/captions.srt")
def render_captions(ctx: Ctx, render_id: str) -> FileResponse:
    rec = get_render(ctx, render_id)
    if not rec.captions_path or not Path(rec.captions_path).exists():
        raise HTTPException(404, "no caption sidecar for this render")
    return FileResponse(Path(rec.captions_path), media_type="text/plain; charset=utf-8", filename=Path(rec.captions_path).name)


@router.delete("/api/v1/renders/{render_id}")
def delete_render(ctx: Ctx, render_id: str) -> dict:
    rec = get_render(ctx, render_id)
    if rec.status == "RENDERING":
        raise HTTPException(409, "render is in progress")
    for p in (rec.output_path, rec.captions_path, rec.detail.get("captions_ass"), rec.detail.get("poster")):
        if p:
            Path(p).unlink(missing_ok=True)
    ctx.renders.delete(render_id)
    return {"deleted": render_id}
