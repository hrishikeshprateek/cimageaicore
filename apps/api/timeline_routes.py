"""The edit, as an object you can work on: build one from the AI's cuts, rearrange it, render it, export it.

The AI does the first pass - proposals from one video, or the best moments across the whole library - and everything
after that is ordinary editing: reorder, trim, reframe, mute, add another clip. Rendering reuses the storyboard
renderer (one branded segment per clip, stitched), so what you see listed is what comes out.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from services.video_composer.timeline import Clip, Timeline

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/timelines", tags=["timelines"])
MAX_SECONDS = 1800.0


def _store(request: Request):
    st = getattr(request.app.state, "timelines", None)
    if st is None:
        raise HTTPException(501, "timelines are not available")
    return st


def _get(request: Request, tid: str) -> Timeline:
    t = _store(request).get(tid)
    if t is None:
        raise HTTPException(404, "timeline not found")
    return t


def _media(request: Request, job_id: str) -> tuple[Any, Path]:
    job = request.app.state.store.get(job_id)
    if job is None:
        raise HTTPException(404, f"job {job_id} not found")
    if not job.source.path or not Path(job.source.path).exists():
        raise HTTPException(400, f"job {job_id} has no local file to cut (analysed from a link?)")
    return job, Path(job.source.path)


def _probe(path: Path) -> tuple[int, int]:
    from services.video_composer import ffmpeg as ff

    try:
        info = ff.probe(path)
        return info.width, info.height
    except Exception:  # noqa: BLE001 - size is only used to suggest a crop
        return 0, 0


# ------------------------------------------------------------------ create
class NewTimeline(BaseModel):
    title: str = ""
    job_id: str | None = Field(default=None, description="seed the edit from this video's proposed cuts")
    from_cuts: bool = True
    preset: Literal["reels", "square", "landscape"] = "reels"
    template: str | None = None
    audio: Literal["source", "voiceover", "both"] = "source"


@router.post("", status_code=status.HTTP_201_CREATED)
def create_timeline(request: Request, body: NewTimeline) -> dict[str, Any]:
    """A new edit. With a job and `from_cuts`, the AI's proposals land on it as clips, ready to be rearranged."""
    st = _store(request)
    clips: list[Clip] = []
    title = body.title
    if body.job_id:
        job, path = _media(request, body.job_id)
        w, h = _probe(path)
        if body.from_cuts:
            from apps.api.composer_routes import _ctx

            ctx = _ctx(request)
            result = request.app.state.store.load_result(job)
            if result is not None:
                from apps.api.composer_routes import _measured_transcript
                from services.video_composer.cuts import propose_cuts

                for c in propose_cuts(result.analysis, job.source.duration_seconds, ctx.limits(),
                                      transcript=_measured_transcript(request, job.id)):
                    clips.append(Clip(job_id=job.id, source_path=str(path), in_seconds=c.in_seconds, out_seconds=c.out_seconds,
                                      source_width=w, source_height=h, captions=list(c.captions), lower_third=c.lower_third,
                                      label=c.title[:60], note=c.reason or ""))
        title = title or (job.source.name or "edit")
    t = Timeline(title=title or "New edit", preset=body.preset, template=body.template, audio=body.audio, clips=clips,
                 source={"kind": "cut" if clips else "manual", "job_id": body.job_id})
    st.put(t)
    request.app.state.store.audit("editor", "timeline.created", "timeline", t.id, {"clips": len(t.clips), "job_id": body.job_id})
    return t.describe()


@router.get("")
def list_timelines(request: Request, job_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    return [{"id": t.id, "title": t.title, "preset": t.preset, "seconds": t.seconds, "clips": len(t.clips), "jobs": t.jobs,
             "created_at": t.created_at.isoformat()} for t in _store(request).list(job_id, min(max(limit, 1), 200))]


@router.get("/{tid}")
def get_timeline(request: Request, tid: str) -> dict[str, Any]:
    return _get(request, tid).describe()


# ------------------------------------------------------------------ edit
class SaveTimeline(BaseModel):
    title: str | None = None
    preset: Literal["reels", "square", "landscape"] | None = None
    template: str | None = None
    fit: Literal["contain", "cover", "auto"] | None = None
    audio: Literal["source", "voiceover", "both"] | None = None
    clips: list[dict[str, Any]] | None = None


@router.put("/{tid}")
def save_timeline(request: Request, tid: str, body: SaveTimeline) -> dict[str, Any]:
    """The whole edit as the studio holds it. Clips are validated and their windows clamped to the source."""
    t = _get(request, tid)
    if body.clips is not None:
        clips: list[Clip] = []
        for raw in body.clips:
            c = Clip.model_validate(raw)
            if c.out_seconds <= c.in_seconds:
                raise HTTPException(400, f"clip {c.label or c.id}: the out point must be after the in point")
            clips.append(c)
        total = sum(c.seconds for c in clips)
        if total > MAX_SECONDS:
            raise HTTPException(400, f"this edit is {total / 60:.0f} minutes long - render it in parts")
        t.clips = clips
    for field_name in ("title", "preset", "template", "fit", "audio"):
        v = getattr(body, field_name)
        if v is not None:
            setattr(t, field_name, v)
    _store(request).put(t)
    return t.describe()


class AddClip(BaseModel):
    job_id: str
    in_seconds: float = Field(ge=0)
    out_seconds: float
    at: int | None = Field(default=None, description="position in the edit; default = the end")
    label: str = ""
    captions_from_transcript: bool = True


@router.post("/{tid}/clips")
def add_clip(request: Request, tid: str, body: AddClip) -> dict[str, Any]:
    """Add a piece of any analysed video to this edit - that is how a reel ends up drawing on several shoots."""
    t = _get(request, tid)
    job, path = _media(request, body.job_id)
    if body.out_seconds <= body.in_seconds:
        raise HTTPException(400, "the out point must be after the in point")
    w, h = _probe(path)
    caps = []
    if body.captions_from_transcript:
        tr = getattr(request.app.state, "transcripts", None)
        measured = tr.get(body.job_id) if tr else None
        if measured:
            from services.video_composer.captions import CaptionCue

            caps = [CaptionCue(start=max(0.0, s.start - body.in_seconds), end=min(body.out_seconds, s.end) - body.in_seconds, text=s.text)
                    for s in measured.between(body.in_seconds, body.out_seconds)]
    clip = Clip(job_id=job.id, source_path=str(path), in_seconds=round(body.in_seconds, 3), out_seconds=round(body.out_seconds, 3),
                source_width=w, source_height=h, captions=caps, label=body.label or (job.source.name or "clip")[:60])
    t.clips.insert(len(t.clips) if body.at is None else max(0, min(body.at, len(t.clips))), clip)
    _store(request).put(t)
    return t.describe()


@router.delete("/{tid}")
def delete_timeline(request: Request, tid: str) -> dict[str, Any]:
    if not _store(request).delete(tid):
        raise HTTPException(404, "timeline not found")
    return {"deleted": tid}


# ------------------------------------------------------------------ render + export
class RenderBody(BaseModel):
    preset: Literal["reels", "square", "landscape"] | None = None
    template: str | None = None


@router.post("/{tid}/render", status_code=status.HTTP_202_ACCEPTED)
def render_timeline(request: Request, tid: str, body: RenderBody) -> dict[str, Any]:
    """Render the edit: one branded segment per clip, stitched, on the composer's queue."""
    from apps.api.composer_routes import _ctx
    from services.video_composer.renderer import RenderSpec
    from services.video_composer.settings import get_composer_settings
    from services.video_composer.store import RenderRecord

    if not get_composer_settings().enabled:
        raise HTTPException(503, "the video composer is disabled - set COMPOSER_ENABLED=true")
    t = _get(request, tid)
    if not t.clips:
        raise HTTPException(400, "this edit has no clips yet")
    ctx = _ctx(request)
    preset = body.preset or t.preset
    try:
        template = ctx.template(body.template or t.template)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if preset not in template.layouts:
        raise HTTPException(400, f"template '{template.name}' has no layout for preset '{preset}'")
    job_id = t.jobs[0] if t.jobs else None
    if job_id is None or request.app.state.store.get(job_id) is None:
        raise HTTPException(400, "every clip of this edit is missing its video")
    job = request.app.state.store.get(job_id)

    segments = []
    for n, c in enumerate(t.clips, start=1):
        segments.append({"n": n, "seconds": c.seconds, "kind": c.kind, "text": c.text, "source_path": c.source_path,
                         "cut_in": c.in_seconds, "note": c.label or c.note, "voice_path": c.voice_path,
                         "focus_x": c.focus_x, "focus_y": c.focus_y, "track": [k.model_dump() for k in c.track], "mute": c.mute})
    spec = RenderSpec(job_id=job_id, source_path=t.clips[0].source_path or "", source_width=t.clips[0].source_width or 1920,
                      source_height=t.clips[0].source_height or 1080, source_has_audio=False, cut_in=0.0, cut_out=t.seconds,
                      preset=preset, template=template.name, fit=t.fit)
    rec = RenderRecord(job_id=job_id, media_id=job.media_id, preset=preset, template=template.name, cut_in=0.0, cut_out=t.seconds,
                       title=t.title or "edit", cut_id=f"timeline:{t.id}", spec=spec,
                       detail={"kind": "storyboard", "timeline_id": t.id, "audio": t.audio, "fit": t.fit,
                               "segments": segments, "warnings": []})
    ctx.renders.create(rec)
    ctx.worker.submit(rec.id)
    request.app.state.store.audit("editor", "timeline.render", "timeline", t.id, {"render": rec.id, "clips": len(t.clips), "preset": preset})
    return {"render_id": rec.id, "job_id": job_id, "clips": len(t.clips), "seconds": t.seconds}


@router.get("/{tid}/export")
def export_timeline(request: Request, tid: str, format: Literal["fcpxml", "edl", "srt", "json"] = "fcpxml") -> Response:
    from services.video_composer.exporters import EXPORTERS

    t = _get(request, tid)
    fn, media_type, ext = EXPORTERS[format]
    name = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in (t.title or "edit")).strip("-")[:50] or "edit"
    return Response(fn(t), media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{name}-{t.id}.{ext}"'})
