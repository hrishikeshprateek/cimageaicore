"""Script writer API: an idea (typed or dictated) -> a timed, shootable script grounded in our own videos.

The agent runs on the shared thread pool like the blog writer: POST returns immediately with status 'generating'
and the page polls until the scenes are there.
"""
from __future__ import annotations

import logging
from typing import Any, Literal

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from agents.script_agent.agent import LANGUAGE_RULES, STYLES, ScriptAgent, scene_plan, word_budget
from apps.api.content_routes import _image_offers
from apps.api.script_store import Script, ScriptStore
from services.tts import TTSError, spoken_seconds

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["scripts"])

Language = Literal["hi", "en", "hinglish"]
LENGTHS = (15, 30, 45, 60, 90, 180, 300, 600)      # what the page offers; any value between 10 s and 30 min is accepted


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()



def _scripts(request: Request) -> ScriptStore:
    st = getattr(request.app.state, "scripts", None)
    if st is None:
        raise HTTPException(501, "scripts need PostgreSQL (set DATABASE_URL)")
    return st


def _agent(request: Request) -> ScriptAgent:
    ag = getattr(request.app.state, "script_agent", None)
    if ag is None:
        raise HTTPException(501, "script writer is not available")
    return ag


def _run(state, sid: str, idea: str, *, language: str, seconds: int, style: str, job_id: str | None, include_images: bool = True) -> None:
    store: ScriptStore = state.scripts
    agent: ScriptAgent = state.script_agent
    images_for = _image_offers(state, idea, include_images=include_images, image_ids=[], anchor_job_id=job_id)

    def work() -> None:
        try:
            res = agent.write(idea, language=language, seconds=seconds, style=style, job_id=job_id, images_for=images_for)
            s = res.script
            store.finish(
                sid, title=s.title, hook=s.hook, cta=s.cta, caption=s.caption,
                scenes=[sc.model_dump() for sc in s.scenes], hashtags=s.hashtags,
                extras={"thumbnail_idea": s.thumbnail_idea, "music_mood": s.music_mood, "shot_list": s.shot_list,
                        "evidence_gaps": s.evidence_gaps, "words": res.words},
                evidence={**res.evidence.model_dump(), "offered_images": [im.id for im in res.offered_images]},
                model=res.model, prompt_version=res.prompt_version, usage=res.usage, warnings=res.warnings, language=s.language,
            )
            if getattr(state, "content", None):
                state.content.record_run("script", draft_id=None, opportunity_id=None, model=res.model, prompt_version=res.prompt_version,
                                         status="ok", usage=res.usage, seconds=res.seconds)
            log.info("script %s ready: %s (%d scenes, %d words, %d warnings)", sid, s.title, len(s.scenes), res.words, len(res.warnings))
        except Exception as exc:  # noqa: BLE001 - the failure belongs on the script, not in the thread pool
            log.exception("script %s failed", sid)
            store.fail(sid, f"{type(exc).__name__}: {exc}")
            if getattr(state, "content", None):
                state.content.record_run("script", draft_id=None, opportunity_id=None, model=agent.model, prompt_version=agent.prompt_version,
                                         status="failed", usage=None, seconds=None, error=str(exc)[:500])

    state.runner.run_async(work)


class ScriptRequest(BaseModel):
    idea: str = Field(min_length=3, description="what the video should be about - typed, or dictated in Hindi")
    language: Language = "hi"
    seconds: int = Field(default=45, ge=10, le=1800)
    style: str = "viral_reel"
    job_id: str | None = Field(default=None, description="anchor the script to one analysed video; null searches the whole library")
    spoken: bool = False
    include_images: bool = True


@router.get("/script-options")
def script_options(request: Request) -> dict[str, Any]:
    """What the Script writer page offers: languages, styles, lengths, and how the mic gets its text."""
    s = request.app.state.settings
    return {
        "languages": [{"key": k, "label": {"hi": "हिंदी (Hindi)", "en": "English", "hinglish": "Hinglish"}[k]} for k in LANGUAGE_RULES],
        "styles": [{"key": k, "label": k.replace("_", " ").title(), "about": v.split(".")[0].removeprefix("Format: ")} for k, v in STYLES.items()],
        "lengths": [{"seconds": n, "label": (f"{n}s" if n < 120 else f"{n // 60} min"), "words": word_budget(n, "hi"), "scenes": scene_plan(n)[0]} for n in LENGTHS],
        "dictation": {"provider": s.stt_provider, "language": s.stt_language},
        "voiceover": {"provider": getattr(getattr(request.app.state, "tts", None), "name", None), "default_voice": s.elevenlabs_voice_id},
        "composer": _composer_ready(),
        "available": getattr(request.app.state, "scripts", None) is not None,
    }


def _composer_ready() -> bool:
    from services.video_composer.settings import get_composer_settings

    return bool(get_composer_settings().enabled)


@router.post("/scripts", status_code=status.HTTP_202_ACCEPTED)
def create_script(request: Request, body: ScriptRequest) -> Script:
    store = _scripts(request)
    _agent(request)
    if body.job_id and request.app.state.store.get(body.job_id) is None:
        raise HTTPException(404, "job not found")
    idea = body.idea.strip()
    sc = store.create(idea, language=body.language, style=body.style, target_seconds=body.seconds, job_id=body.job_id, spoken=body.spoken)
    request.app.state.store.audit("editor", "script.requested", "script", sc.id,
                                  {"seconds": body.seconds, "language": body.language, "style": body.style, "spoken": body.spoken})
    _run(request.app.state, sc.id, idea, language=body.language, seconds=body.seconds, style=body.style, job_id=body.job_id,
         include_images=body.include_images)
    return sc


@router.get("/scripts")
def list_scripts(request: Request, status: str | None = None) -> list[Script]:
    return _scripts(request).list(status)


@router.get("/scripts/{sid}")
def get_script(request: Request, sid: str) -> dict:
    s = _scripts(request).get(sid)
    if s is None:
        raise HTTPException(404, "script not found")
    return {**s.model_dump(mode="json"), "planned_seconds": s.planned_seconds, "words": s.words}


@router.get("/scripts/{sid}/versions")
def script_versions(request: Request, sid: str) -> list[dict]:
    return _scripts(request).versions(sid)


@router.get("/scripts/{sid}/export")
def export_script(request: Request, sid: str, format: Literal["md", "txt"] = "md") -> Response:
    s = _scripts(request).get(sid)
    if s is None:
        raise HTTPException(404, "script not found")
    text = s.as_markdown() if format == "md" else s.teleprompter()
    name = (s.title or "script").lower().replace(" ", "-")[:50]
    return Response(text, media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}.{format}"'})


class ScriptEdit(BaseModel):
    title: str | None = None
    hook: str | None = None
    cta: str | None = None
    caption: str | None = None
    hashtags: list[str] | None = None
    scenes: list[dict[str, Any]] | None = None


@router.put("/scripts/{sid}")
def edit_script(request: Request, sid: str, body: ScriptEdit) -> Script:
    store = _scripts(request)
    s = store.get(sid)
    if s is None:
        raise HTTPException(404, "script not found")
    if s.status == "generating":
        raise HTTPException(409, "the writer is still working on this script")
    scenes = body.scenes
    if scenes is not None:
        for i, sc in enumerate(scenes, start=1):
            sc["n"] = i
            sc["seconds"] = float(sc.get("seconds") or 0)
    out = store.update(sid, title=body.title, hook=body.hook, cta=body.cta, caption=body.caption, scenes=scenes, hashtags=body.hashtags)
    request.app.state.store.audit("editor", "script.edited", "script", sid, {"version": out.version})
    return out


@router.post("/scripts/{sid}/regenerate", status_code=status.HTTP_202_ACCEPTED)
def regenerate_script(request: Request, sid: str, seconds: int | None = None, language: Language | None = None, style: str | None = None) -> Script:
    store = _scripts(request)
    _agent(request)
    s = store.get(sid)
    if s is None:
        raise HTTPException(404, "script not found")
    if s.status == "generating":
        raise HTTPException(409, "already running")
    s = store.reset_for_regeneration(sid, language=language, style=style, target_seconds=seconds)
    request.app.state.store.audit("editor", "script.regenerate", "script", sid, {"seconds": s.target_seconds, "language": s.language, "style": s.style})
    _run(request.app.state, sid, s.idea, language=s.language, seconds=s.target_seconds, style=s.style, job_id=s.job_id)
    return s


class StatusBody(BaseModel):
    status: Literal["new", "in_review", "approved", "rejected"]


@router.post("/scripts/{sid}/status")
def set_script_status(request: Request, sid: str, body: StatusBody) -> Script:
    store = _scripts(request)
    if store.get(sid) is None:
        raise HTTPException(404, "script not found")
    out = store.set_status(sid, body.status)
    request.app.state.store.audit("editor", f"script.{body.status}", "script", sid, {"version": out.version})
    return out


@router.delete("/scripts/{sid}")
def delete_script(request: Request, sid: str) -> dict:
    if not _scripts(request).delete(sid):
        raise HTTPException(404, "script not found")
    request.app.state.store.audit("editor", "script.deleted", "script", sid, {})
    return {"deleted": sid}


# ------------------------------------------------------------------ voiceover (ElevenLabs)
def _tts(request: Request):
    t = getattr(request.app.state, "tts", None)
    if t is None:
        raise HTTPException(501, "voiceovers are off - set TTS_PROVIDER=elevenlabs and ELEVENLABS_API_KEY in .env")
    return t


def _voice_dir(request: Request, sid: str) -> Path:
    d = request.app.state.settings.voiceovers_dir / sid
    d.mkdir(parents=True, exist_ok=True)
    return d


def audio_seconds(path: Path) -> float:
    """How long the spoken line actually is - the scene has to be at least this long."""
    import shutil
    import subprocess

    ffprobe = shutil.which("ffprobe")
    if ffprobe is None or not path.exists():
        return 0.0
    r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, check=False)
    try:
        return round(float(r.stdout.strip()), 2)
    except ValueError:
        return 0.0


@router.get("/tts/voices")
def tts_voices(request: Request) -> dict[str, Any]:
    """The voices on the account, so the page can offer them. Also says how many characters are left."""
    t = getattr(request.app.state, "tts", None)
    if t is None:
        return {"provider": None, "voices": [], "usage": {}, "default": ""}
    try:
        voices = [v.as_dict() for v in t.voices()]
    except TTSError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"provider": t.name, "voices": voices, "usage": t.usage(), "default": request.app.state.settings.elevenlabs_voice_id or (voices[0]["id"] if voices else "")}


class VoiceoverBody(BaseModel):
    voice_id: str | None = None
    scenes: list[int] | None = Field(default=None, description="only these scene numbers; default = the whole script")
    fit_scenes: bool = Field(default=True, description="stretch a scene that is shorter than its spoken line")


@router.post("/scripts/{sid}/voiceover")
def make_voiceover(request: Request, sid: str, body: VoiceoverBody) -> dict[str, Any]:
    """Speak every scene with ElevenLabs. One file per scene, so the voice stays inside its own shot."""
    store = _scripts(request)
    tts = _tts(request)
    sc = store.get(sid)
    if sc is None:
        raise HTTPException(404, "script not found")
    if sc.status == "generating":
        raise HTTPException(409, "the writer is still working on this script")
    voice_id = (body.voice_id or request.app.state.settings.elevenlabs_voice_id or "").strip()
    out_dir = _voice_dir(request, sid)
    wanted = set(body.scenes or [])
    existing = {int(x["n"]): x for x in ((sc.extras.get("voiceover") or {}).get("scenes") or [])}
    lines, spent = [], 0
    for scene in sc.scenes:
        n = int(scene.get("n") or 0)
        text = (scene.get("voiceover") or "").strip()
        if wanted and n not in wanted:
            if n in existing:
                lines.append(existing[n])
            continue
        if not text:
            continue
        path = out_dir / f"scene_{n:03d}.mp3"
        try:
            tts.speak(text, path, voice_id=voice_id or None)
        except TTSError as exc:
            raise HTTPException(502, f"scene {n}: {exc}") from exc
        spent += len(text)
        lines.append({"n": n, "path": str(path), "seconds": audio_seconds(path), "chars": len(text), "text": text})
    if not lines:
        raise HTTPException(400, "no voiceover lines to speak")
    lines.sort(key=lambda x: x["n"])
    by_scene = {int(x["n"]): x for x in lines}
    warnings = [f"scene {x['n']}: the line takes {x['seconds']:.1f}s but the scene is {float(next((s.get('seconds') or 0) for s in sc.scenes if int(s.get('n') or 0) == x['n'])):.1f}s"
                for x in lines if x["seconds"] > float(next((s.get("seconds") or 0) for s in sc.scenes if int(s.get("n") or 0) == x["n"])) + 0.35]
    scenes = None
    if body.fit_scenes and warnings:
        scenes = [{**s, "seconds": max(float(s.get("seconds") or 0), by_scene[int(s.get("n") or 0)]["seconds"] + 0.3)} if int(s.get("n") or 0) in by_scene else s
                  for s in sc.scenes]
        warnings = [w + " - the scene was stretched to fit" for w in warnings]
    vo = {"provider": tts.name, "voice_id": voice_id, "scenes": lines, "seconds": round(sum(x["seconds"] for x in lines), 2),
          "chars": sum(x["chars"] for x in lines), "warnings": warnings, "made_at": _now_iso()}
    out = store.update(sid, scenes=scenes, extras={**sc.extras, "voiceover": vo}, edited_by="editor:voiceover")
    request.app.state.store.audit("editor", "script.voiceover", "script", sid, {"scenes": len(lines), "chars": spent, "voice": voice_id, "provider": tts.name})
    return {**out.model_dump(mode="json"), "planned_seconds": out.planned_seconds, "words": out.words}


@router.get("/scripts/{sid}/audio/{scene}")
def scene_audio(request: Request, sid: str, scene: int) -> FileResponse:
    sc = _scripts(request).get(sid)
    if sc is None:
        raise HTTPException(404, "script not found")
    line = next((x for x in ((sc.extras.get("voiceover") or {}).get("scenes") or []) if int(x["n"]) == scene), None)
    if line is None or not Path(line["path"]).exists():
        raise HTTPException(404, "no voiceover for this scene yet")
    return FileResponse(line["path"], media_type="audio/mpeg", headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------ script -> video (the reel renderer, scene by scene)
class VideoBody(BaseModel):
    preset: Literal["reels", "square", "landscape"] = "reels"
    template: str | None = None
    audio: Literal["voiceover", "both", "source"] = "voiceover"
    fit: Literal["contain", "cover", "auto"] | None = None


@router.post("/scripts/{sid}/video", status_code=status.HTTP_202_ACCEPTED)
def make_video(request: Request, sid: str, body: VideoBody) -> dict[str, Any]:
    """Cut the script together from the footage it points at, with the branded template and the voiceover over it."""
    from apps.api.composer_routes import _ctx
    from services.video_composer.settings import get_composer_settings
    from services.video_composer.storyboard import plan
    from services.video_composer.store import RenderRecord
    from services.video_composer.renderer import RenderSpec

    if not get_composer_settings().enabled:
        raise HTTPException(503, "the video composer is disabled - set COMPOSER_ENABLED=true in .env")
    store = _scripts(request)
    sc = store.get(sid)
    if sc is None:
        raise HTTPException(404, "script not found")
    if sc.status == "generating":
        raise HTTPException(409, "the writer is still working on this script")
    ctx = _ctx(request)
    images = getattr(request.app.state, "images", None)
    voice = {int(x["n"]): x["path"] for x in ((sc.extras.get("voiceover") or {}).get("scenes") or [])}

    def image_path(image_id: str) -> Path | None:
        rec = images.get(image_id) if images else None
        return Path(rec.path) if rec else None

    try:
        board = plan(sc, job_for=request.app.state.store.get, image_path=image_path,
                     voice_for=lambda n: Path(voice[n]) if n in voice and Path(voice[n]).exists() else None)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    job_id = board.job_id or sc.job_id
    if job_id is None or request.app.state.store.get(job_id) is None:
        raise HTTPException(400, "this script points at no footage on this machine - pick stills for the scenes first")
    try:
        template = ctx.template(body.template)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if body.preset not in template.layouts:
        raise HTTPException(400, f"template '{template.name}' has no layout for preset '{body.preset}'")
    if body.audio != "source" and not voice:
        raise HTTPException(400, "no voiceover yet - generate it first, or render with the footage's own sound")

    job = request.app.state.store.get(job_id)
    spec = RenderSpec(job_id=job_id, source_path=board.segments[0].source_path or "", source_width=1920, source_height=1080,
                      source_has_audio=False, cut_in=0.0, cut_out=board.seconds, preset=body.preset, template=template.name, fit=body.fit)
    rec = RenderRecord(job_id=job_id, media_id=job.media_id, preset=body.preset, template=template.name, cut_in=0.0, cut_out=board.seconds,
                       title=sc.title, cut_id=f"script:{sid}", spec=spec,
                       detail={"kind": "storyboard", "script_id": sid, "audio": body.audio, "fit": body.fit,
                               "segments": [vars(seg) for seg in board.segments], "warnings": board.warnings})
    ctx.renders.create(rec)
    ctx.worker.submit(rec.id)
    request.app.state.store.audit("editor", "script.video", "script", sid, {"render": rec.id, "preset": body.preset, "scenes": len(board.segments), "audio": body.audio})
    return {"render_id": rec.id, "job_id": job_id, "scenes": len(board.segments), "seconds": board.seconds, "warnings": board.warnings}


@router.get("/scripts/{sid}/video")
def script_videos(request: Request, sid: str) -> list[dict[str, Any]]:
    """Renders made from this script, newest first (the same records the Reels studio lists)."""
    from apps.api.composer_routes import _ctx
    from services.video_composer.settings import get_composer_settings

    if not get_composer_settings().enabled:
        return []
    ctx = _ctx(request)
    out = []
    for r in ctx.renders.list(None, 200):
        if (r.detail or {}).get("script_id") != sid:
            continue
        out.append({"id": r.id, "status": r.status, "preset": r.preset, "template": r.template, "duration": r.duration_seconds,
                    "size_bytes": r.size_bytes, "error": r.error, "created_at": r.created_at.isoformat(),
                    "progress": (r.detail or {}).get("progress"), "warnings": (r.detail or {}).get("warnings") or [],
                    "scenes": (r.detail or {}).get("scenes") or [], "audio": (r.detail or {}).get("audio"),
                    "url": f"/api/v1/renders/{r.id}/video" if r.status == "DONE" else None,
                    "poster": f"/api/v1/renders/{r.id}/poster.jpg" if r.status == "DONE" else None})
    return out
