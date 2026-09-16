"""Content layer API: opportunities queue, blog drafts, review states."""
from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from agents.blog_agent.agent import BlogAgent, image_marker_ids
from agents.blog_agent.schemas import ImagePlacement
from apps.api.content_store import ContentStore, Draft, Opportunity
from services.media_library.frames import FrameError
from services.media_library.store import IMAGE_EXTENSIONS, ImageRecord, ImageStore

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["content"])


def _content(request: Request) -> ContentStore:
    cs = getattr(request.app.state, "content", None)
    if cs is None:
        raise HTTPException(501, "content features need PostgreSQL (set DATABASE_URL)")
    return cs


def _images(request: Request) -> ImageStore:
    st = getattr(request.app.state, "images", None)
    if st is None:
        raise HTTPException(501, "images need PostgreSQL (set DATABASE_URL)")
    return st


Depth = Literal["standard", "in_depth"]
DEFAULT_WORDS = {"standard": 1000, "in_depth": 1500}


# ------------------------------------------------------------------ opportunities
class ManualOpportunity(BaseModel):
    title: str = Field(min_length=3)
    reason: str | None = None
    suggested_formats: list[str] = Field(default_factory=lambda: ["blog", "linkedin", "instagram"])
    audience: str | None = None
    job_id: str | None = None


@router.get("/opportunities")
def list_opportunities(request: Request, status: str | None = None) -> list[Opportunity]:
    return _content(request).list_opportunities(status)


@router.post("/opportunities", status_code=status.HTTP_201_CREATED)
def create_opportunity(request: Request, body: ManualOpportunity) -> Opportunity:
    return _content(request).create_manual_opportunity(body.title, body.reason, body.suggested_formats, body.audience, body.job_id)


class StatusChange(BaseModel):
    status: Literal["new", "accepted", "dismissed"]


@router.post("/opportunities/{oid}/status")
def set_opportunity_status(request: Request, oid: str, body: StatusChange) -> Opportunity:
    opp = _content(request).set_opportunity_status(oid, body.status)
    if opp is None:
        raise HTTPException(404, "opportunity not found")
    return opp


# ------------------------------------------------------------------ drafts
class DraftRequest(BaseModel):
    opportunity_id: str | None = None
    brief: str | None = Field(default=None, description="Free-text brief when not drafting from an opportunity.")
    job_id: str | None = Field(default=None, description="Anchor the article on this analysed video (all its blocks become evidence, its frames become pictures).")
    depth: Depth = "in_depth"
    target_words: int | None = Field(default=None, ge=200, le=3000, description="Blank = 1000 for standard, 1500 for in-depth.")
    include_images: bool = True
    image_ids: list[str] = Field(default_factory=list, description="Pictures the writer must be offered in addition to the automatic ones.")


def _job_ids_in(evidence, first: str | None) -> list[str]:
    ids: list[str] = [first] if first else []
    for b in evidence.blocks:
        jid = b.block_id.split(":", 1)[0]
        if jid and jid not in ids:
            ids.append(jid)
    return ids


def _image_offers(state, brief: str, *, include_images: bool, image_ids: list[str], anchor_job_id: str | None):
    """Build the `images_for(evidence)` callback: frames of the evidence videos (extracted on demand) + matching library photos."""
    images: ImageStore | None = getattr(state, "images", None)
    store = state.store
    if not include_images or images is None:
        return None

    def images_for(evidence) -> list[ImageRecord]:
        job_ids = _job_ids_in(evidence, anchor_job_id)[:3]
        for jid in job_ids:
            job = store.get(jid)
            if job is None:
                continue
            try:
                _ensure_pictures(state, job, force=False)
            except Exception as exc:  # noqa: BLE001 - pictures are optional, the article is not
                log.warning("pictures for %s failed: %s", jid, exc)
        offers = images.offers(job_ids=job_ids, brief=brief)
        if image_ids:
            wanted = images.get_many(image_ids)
            offers = [wanted[i] for i in image_ids if i in wanted] + [o for o in offers if o.id not in wanted]
        return offers

    return images_for


def _ensure_pictures(state, job, *, force: bool, mode: str = "ai") -> tuple[list[ImageRecord], dict]:
    """Candidate pictures for a job: AI-verified stills (shot detection + gateway description) when the job has a local file,
    the YouTube thumbnail when it was analysed from a URL; timestamp-based frames as the no-AI fallback (mode='blocks')."""
    images: ImageStore = state.images
    store = state.store
    settings = state.settings
    if not job.source.path or not Path(job.source.path).exists():
        title = None
        for b in store.blocks(job.id, "video"):
            title = b.payload.get("title")
        rec = images.youtube_thumbnail_for_job(job, title=title)
        return ([rec] if rec else []), {"mode": "youtube_thumbnail"}
    blocks = store.blocks(job.id)
    if mode == "blocks":
        return images.frames_for_job(job, blocks, force=force), {"mode": "blocks"}
    provider = state.engine.provider
    try:
        reg = getattr(state, "prompts", None)
        recs, meta = images.index_job(job, blocks, provider, force=force, institution_context=reg.institution_context if reg else settings.institution_context, model=settings.blog_model or None)
        return recs, {"mode": "ai", **meta}
    except Exception as exc:  # noqa: BLE001 - no vision (or a provider error): fall back to timestamp-based frames
        log.warning("AI still index for %s failed (%s); using timestamp-based frames", job.id, exc)
        return images.frames_for_job(job, blocks, force=force), {"mode": "blocks", "fallback_reason": f"{type(exc).__name__}: {exc}"[:300]}


def _run_agent(state, draft_id: str, brief: str, opp: Opportunity | None, target_words: int, *, depth: str = "standard",
               job_id: str | None = None, include_images: bool = True, image_ids: list[str] | None = None) -> None:
    cs: ContentStore = state.content
    agent: BlogAgent = state.blog_agent
    anchor = job_id or (opp.job_id if opp else None)
    images_for = _image_offers(state, brief, include_images=include_images, image_ids=image_ids or [], anchor_job_id=anchor)

    def work() -> None:
        try:
            res = agent.draft(
                brief,
                job_id=anchor,
                extra_queries=[opp.reason] if opp and opp.reason else None,
                formats=opp.suggested_formats if opp else None,
                target_words=target_words,
                depth=depth,
                images_for=images_for,
            )
            d = res.draft
            cs.finish_draft(
                draft_id, title=d.title, slug=d.slug, body_markdown=d.body_markdown,
                seo={"seo_title": d.seo_title, "meta_description": d.meta_description, "excerpt": d.excerpt, "tags": d.tags, "evidence_gaps": d.evidence_gaps},
                social=d.social.model_dump(), citations=[c.model_dump() for c in d.citations],
                evidence={**res.evidence.model_dump(), "offered_images": [im.id for im in res.offered_images]},
                hero_block_id=d.hero_block_id, model=res.model, prompt_version=res.prompt_version, usage=res.usage, warnings=res.warnings,
                images=[p.model_dump() for p in d.images], depth=res.depth,
            )
            cs.record_run("blog", draft_id=draft_id, opportunity_id=opp.id if opp else None, model=res.model, prompt_version=res.prompt_version,
                          status="ok", usage=res.usage, seconds=res.seconds)
            log.info("draft %s ready: %s (%d citations, %d warnings)", draft_id, d.title, len(d.citations), len(res.warnings))
        except Exception as exc:  # noqa: BLE001
            log.exception("draft %s failed", draft_id)
            cs.fail_draft(draft_id, f"{type(exc).__name__}: {exc}")
            cs.record_run("blog", draft_id=draft_id, opportunity_id=opp.id if opp else None, model=agent.model, prompt_version=agent.prompt_version,
                          status="failed", usage=None, seconds=None, error=str(exc)[:500])

    state.runner.run_async(work)


def auto_draft_job(state, job_id: str) -> list[str]:
    """AUTO_DRAFT: after a video's opportunities are extracted, turn the best ones into blog drafts for review (no human click needed).
    Returns the draft ids created. Never raises - the analysis job is already complete and must stay that way."""
    settings = state.settings
    cs: ContentStore | None = getattr(state, "content", None)
    if cs is None or not settings.auto_draft:
        return []
    created: list[str] = []
    try:
        opps = [o for o in cs.list_opportunities(status="new") if o.job_id == job_id and (o.confidence or 0) >= settings.auto_draft_min_confidence]
        opps.sort(key=lambda o: o.confidence or 0, reverse=True)
        for opp in opps[: max(0, settings.auto_draft_max_per_video)]:
            cs.set_opportunity_status(opp.id, "accepted")
            depth = settings.auto_draft_depth if settings.auto_draft_depth in DEFAULT_WORDS else "standard"
            draft = cs.create_draft(opp.title, opp.id, depth=depth)
            state.store.audit("auto-draft", "draft.auto_created", "draft", draft.id, {"opportunity_id": opp.id, "job_id": job_id, "confidence": opp.confidence})
            _run_agent(state, draft.id, opp.title, opp, DEFAULT_WORDS[depth], depth=depth, job_id=job_id)
            created.append(draft.id)
        log.info("auto-draft for job %s: %d draft(s) from %d opportunit%s", job_id, len(created), len(opps), "y" if len(opps) == 1 else "ies")
    except Exception:  # noqa: BLE001
        log.exception("auto-draft for job %s failed", job_id)
    return created


@router.post("/drafts", status_code=status.HTTP_202_ACCEPTED)
def create_draft(request: Request, body: DraftRequest) -> Draft:
    cs = _content(request)
    opp = None
    if body.opportunity_id:
        opp = cs.get_opportunity(body.opportunity_id)
        if opp is None:
            raise HTTPException(404, "opportunity not found")
        if opp.status == "dismissed":
            raise HTTPException(409, "opportunity was dismissed")
    brief = (body.brief or "").strip() or (opp.title if opp else "")
    if not brief:
        raise HTTPException(400, "give a brief or an opportunity_id")
    if body.job_id and request.app.state.store.get(body.job_id) is None:
        raise HTTPException(404, "job not found")
    draft = cs.create_draft(brief, opp.id if opp else None, depth=body.depth)
    _run_agent(request.app.state, draft.id, brief, opp, body.target_words or DEFAULT_WORDS[body.depth], depth=body.depth, job_id=body.job_id,
               include_images=body.include_images, image_ids=body.image_ids)
    return draft


@router.get("/drafts")
def list_drafts(request: Request, status: str | None = None) -> list[Draft]:
    return _content(request).list_drafts(status)


@router.get("/drafts/{did}")
def get_draft(request: Request, did: str) -> dict:
    d = _content(request).get_draft(did)
    if d is None:
        raise HTTPException(404, "draft not found")
    return {**d.model_dump(mode="json"), "body_markdown_clean": d.body_markdown_clean}


@router.get("/drafts/{did}/versions")
def draft_versions(request: Request, did: str) -> list[dict]:
    return _content(request).draft_versions(did)


@router.post("/drafts/{did}/regenerate", status_code=status.HTTP_202_ACCEPTED)
def regenerate_draft(request: Request, did: str, target_words: int | None = None, depth: Depth | None = None, include_images: bool = True) -> Draft:
    cs = _content(request)
    d = cs.get_draft(did)
    if d is None:
        raise HTTPException(404, "draft not found")
    if d.status == "generating":
        raise HTTPException(409, "already generating")
    opp = cs.get_opportunity(d.opportunity_id) if d.opportunity_id else None
    depth = depth or d.depth
    anchor = d.evidence.get("job_id") if isinstance(d.evidence, dict) else None
    keep = [im["image_id"] for im in d.images if im.get("image_id")]   # pictures already in the draft stay on offer
    cs.reset_draft_for_regeneration(did, depth=depth)
    _run_agent(request.app.state, did, d.brief, opp, target_words or DEFAULT_WORDS.get(depth, 1000), depth=depth, job_id=anchor, include_images=include_images, image_ids=keep)
    return cs.get_draft(did)


class DraftImages(BaseModel):
    images: list[ImagePlacement]
    body_markdown: str | None = Field(default=None, description="Optional: the body with [img=...] lines moved/added by the editor.")


@router.put("/drafts/{did}/images")
def set_draft_images(request: Request, did: str, body: DraftImages) -> dict:
    """Editor picks the hero / inline pictures. Inline ones missing from the body get a marker appended at the end of the article."""
    cs = _content(request)
    images = _images(request)
    d = cs.get_draft(did)
    if d is None:
        raise HTTPException(404, "draft not found")
    if d.status in ("generating", "failed"):
        raise HTTPException(409, f"draft is {d.status}")
    known = images.get_many([p.image_id for p in body.images])
    missing = [p.image_id for p in body.images if p.image_id not in known]
    if missing:
        raise HTTPException(400, f"unknown image ids: {', '.join(missing)}")
    heroes = [p for p in body.images if p.placement == "hero"]
    if len(heroes) > 1:
        raise HTTPException(400, "only one hero image")
    md = body.body_markdown if body.body_markdown is not None else (d.body_markdown or "")
    present = set(image_marker_ids(md))
    wanted = {p.image_id for p in body.images if p.placement == "inline"}
    for p in body.images:
        if p.placement == "inline" and p.image_id not in present:
            md = md.rstrip() + f"\n\n[img={p.image_id}]\n"
    # markers for pictures no longer wanted disappear
    import re as _re

    md = _re.sub(r"^[ \t]*\[img=([^\]]+)\][ \t]*\n?", lambda m: "" if m.group(1).strip() not in wanted else m.group(0), md, flags=_re.MULTILINE)
    updated = cs.update_draft_images(did, [p.model_dump() for p in body.images], body_markdown=md)
    return {**updated.model_dump(mode="json"), "body_markdown_clean": updated.body_markdown_clean}


class DraftEdit(BaseModel):
    title: str | None = None
    body_markdown: str | None = None


@router.put("/drafts/{did}")
def edit_draft(request: Request, did: str, body: DraftEdit) -> Draft:
    cs = _content(request)
    d = cs.get_draft(did)
    if d is None:
        raise HTTPException(404, "draft not found")
    if d.status in ("generating", "failed"):
        raise HTTPException(409, f"draft is {d.status}")
    return cs.update_draft_text(did, title=body.title, body_markdown=body.body_markdown)


class DraftStatusChange(BaseModel):
    status: Literal["in_review", "approved", "rejected"]


@router.post("/drafts/{did}/status")
def set_draft_status(request: Request, did: str, body: DraftStatusChange) -> Draft:
    cs = _content(request)
    d = cs.get_draft(did)
    if d is None:
        raise HTTPException(404, "draft not found")
    if d.status in ("generating", "failed"):
        raise HTTPException(409, f"draft is {d.status}")
    request.app.state.store.audit("editor", f"draft.{body.status}", "draft", did, {"from": d.status})
    updated = cs.set_draft_status(did, body.status)
    if body.status == "approved":
        from apps.api.publish_routes import auto_publish

        try:
            queued = auto_publish(request, updated)
            if queued:
                log.info("draft %s approved -> publishing to %d target(s)", did, len(queued))
        except Exception as exc:  # noqa: BLE001 - approval must succeed even if publishing cannot start
            log.exception("auto-publish for %s could not start: %s", did, exc)
    return updated


@router.get("/agent-runs")
def agent_runs(request: Request) -> list[dict]:
    return _content(request).list_runs()


# ------------------------------------------------------------------ images (video frames + local library; never the open internet)
class ImageOut(BaseModel):
    id: str
    kind: str
    job_id: str | None
    block_id: str | None
    timestamp: str | None
    timestamp_seconds: float | None
    width: int | None
    height: int | None
    size_bytes: int | None
    description: str
    tags: list[str]
    suitable_for: list[str]
    source_name: str | None
    url: str
    created_at: str

    @classmethod
    def of(cls, r: ImageRecord) -> "ImageOut":
        return cls(id=r.id, kind=r.kind, job_id=r.job_id, block_id=r.block_id, timestamp=r.timestamp, timestamp_seconds=r.timestamp_seconds, width=r.width,
                   height=r.height, size_bytes=r.size_bytes, description=r.description, tags=r.tags, suitable_for=r.suitable_for, source_name=r.source_name,
                   url=f"/api/v1/images/{r.id}", created_at=r.created_at.isoformat())


@router.get("/images")
def list_images(request: Request, job_id: str | None = None, kind: str | None = None, ids: str | None = None, limit: int = 300) -> list[ImageOut]:
    images = _images(request)
    if ids:
        wanted = [i.strip() for i in ids.split(",") if i.strip()]
        found = images.get_many(wanted)
        return [ImageOut.of(found[i]) for i in wanted if i in found]
    return [ImageOut.of(r) for r in images.list(job_id=job_id, kind=kind, limit=min(max(limit, 1), 1000))]


@router.get("/images/{image_id}/meta")
def image_meta(request: Request, image_id: str) -> ImageOut:
    r = _images(request).get(image_id)
    if r is None:
        raise HTTPException(404, "image not found")
    return ImageOut.of(r)


@router.get("/images/{image_id}")
def image_file(request: Request, image_id: str) -> FileResponse:
    r = _images(request).get(image_id)
    if r is None or not Path(r.path).exists():
        raise HTTPException(404, "image not found")
    ext = Path(r.path).suffix.lower()
    media_type = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}.get(ext, "application/octet-stream")
    return FileResponse(Path(r.path), media_type=media_type, headers={"Cache-Control": "private, max-age=3600"})


class ImageEdit(BaseModel):
    description: str | None = None
    tags: list[str] | None = None
    suitable_for: list[str] | None = None


@router.patch("/images/{image_id}")
def edit_image(request: Request, image_id: str, body: ImageEdit) -> ImageOut:
    r = _images(request).update(image_id, description=body.description, tags=body.tags, suitable_for=body.suitable_for)
    if r is None:
        raise HTTPException(404, "image not found")
    return ImageOut.of(r)


@router.delete("/images/{image_id}")
def delete_image(request: Request, image_id: str) -> dict:
    if not _images(request).delete(image_id):
        raise HTTPException(404, "image not found")
    return {"deleted": image_id}


class FramesResult(BaseModel):
    mode: str
    meta: dict
    images: list[ImageOut]


@router.post("/jobs/{job_id}/frames", status_code=status.HTTP_201_CREATED)
def extract_job_frames(request: Request, job_id: str, mode: Literal["ai", "blocks"] = "ai", force: bool = False) -> FramesResult:
    """Candidate pictures for a video. mode=ai (default): shot detection + the gateway describes what each still really shows,
    keeping only usable ones (accurate descriptions; the analysis timestamps are only approximate). mode=blocks: one quick frame
    per media / key_moment block at its timestamp, no AI. Online (YouTube) jobs get the video's own thumbnail."""
    _images(request)
    store = request.app.state.store
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    if job.source.path and not store.blocks(job_id):
        raise HTTPException(409, f"job {job_id} has no blocks yet (state={job.state})")
    recs, meta = _ensure_pictures(request.app.state, job, force=force, mode=mode)
    if not recs and not job.source.path:
        raise HTTPException(400, "this job has no local video file and no YouTube thumbnail could be fetched")
    return FramesResult(mode=meta.pop("mode", mode), meta=meta, images=[ImageOut.of(r) for r in recs])


@router.post("/jobs/{job_id}/frames/at", status_code=status.HTTP_201_CREATED)
def extract_frame_at(request: Request, job_id: str, at: float, description: str = "") -> ImageOut:
    """An editor-chosen still at `at` seconds (e.g. from the composer's trim preview)."""
    job = request.app.state.store.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    try:
        return ImageOut.of(_images(request).frame_at(job, max(0.0, at), description=description))
    except FrameError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/images/library", status_code=status.HTTP_201_CREATED)
async def upload_library_image(
    request: Request,
    file: Annotated[UploadFile, File()],
    description: Annotated[str, Form()] = "",
    tags: Annotated[str, Form()] = "",
    suitable_for: Annotated[str, Form()] = "",
) -> ImageOut:
    """Add a photo to the curated library (the media team's own pictures). `tags` / `suitable_for` are comma separated."""
    images = _images(request)
    name = Path(file.filename or "image.jpg").name
    if Path(name).suffix.lower() not in IMAGE_EXTENSIONS:
        raise HTTPException(400, f"unsupported image type; use {', '.join(sorted(IMAGE_EXTENSIONS))}")
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / name
        with dest.open("wb") as out:
            while chunk := await file.read(8 * 1024 * 1024):
                out.write(chunk)
        try:
            rec = images.add_library_file(dest, description=description, tags=[t for t in tags.split(",") if t.strip()],
                                          suitable_for=[t.strip() for t in suitable_for.split(",") if t.strip()], original_name=name, move=True)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc
    return ImageOut.of(rec)


class LibraryImport(BaseModel):
    path: str = Field(description="A folder under NAS_ALLOWED_ROOTS holding jpg/png/webp files (optionally captions.txt: name<TAB>description).")
    tags: list[str] = Field(default_factory=list)


@router.post("/images/library/import", status_code=status.HTTP_201_CREATED)
def import_library_folder(request: Request, body: LibraryImport) -> list[ImageOut]:
    settings = request.app.state.settings
    folder = Path(body.path).expanduser()
    try:
        resolved = folder.resolve(strict=True)
    except FileNotFoundError as exc:
        raise HTTPException(404, f"folder not found: {body.path}") from exc
    if not resolved.is_dir():
        raise HTTPException(400, "path is not a folder")
    if not any(resolved == root or root in resolved.parents for root in settings.allowed_roots):
        raise HTTPException(400, f"folder is outside the allowed roots ({', '.join(str(r) for r in settings.allowed_roots)})")
    return [ImageOut.of(r) for r in _images(request).import_folder(resolved, tags=body.tags)]
