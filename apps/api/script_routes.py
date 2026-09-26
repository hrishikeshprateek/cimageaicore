"""Script writer API: an idea (typed or dictated) -> a timed, shootable script grounded in our own videos.

The agent runs on the shared thread pool like the blog writer: POST returns immediately with status 'generating'
and the page polls until the scenes are there.
"""
from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from agents.script_agent.agent import LANGUAGE_RULES, STYLES, ScriptAgent, scene_plan, word_budget
from apps.api.content_routes import _image_offers
from apps.api.script_store import Script, ScriptStore

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["scripts"])

Language = Literal["hi", "en", "hinglish"]
LENGTHS = (15, 30, 45, 60, 90, 180, 300, 600)      # what the page offers; any value between 10 s and 30 min is accepted


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
        "available": getattr(request.app.state, "scripts", None) is not None,
    }


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
