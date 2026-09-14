from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status

from apps.api.jobs import Job, JobState
from services.block_engine import sources
from services.block_engine.media import ffprobe_available
from services.block_engine.schemas import AnalysisResult, Block, SearchHit

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1")

CHUNK = 8 * 1024 * 1024


@router.get("/system")
def system(request: Request) -> dict:
    s = request.app.state.settings
    engine = request.app.state.engine
    return {
        "app": s.app_name,
        "version": s.app_version,
        "provider": engine.provider.name,
        "model": engine.provider.model,
        "store": request.app.state.store.kind,
        "embedder": request.app.state.embedder.name,
        "embedding_model": request.app.state.embedder.model,
        "embedding_dimensions": request.app.state.embedder.dimensions,
        "vectors": request.app.state.store.supports_vectors,
        "embeddings": request.app.state.store.embedding_stats(),
        "prompt_version": s.prompt_version,
        "ffprobe": ffprobe_available(),
        "allowed_roots": [str(r) for r in s.allowed_roots],
        "max_upload_mb": s.max_upload_mb,
        "states": [st.value for st in JobState],
    }


@router.post("/analyze", status_code=status.HTTP_202_ACCEPTED)
async def analyze(
    request: Request,
    file: Annotated[UploadFile | None, File()] = None,
    path: Annotated[str | None, Form()] = None,
    url: Annotated[str | None, Form()] = None,
) -> dict:
    """Start a video analysis job. Provide exactly one of: file (upload), path (allow-listed NAS path), url (YouTube)."""
    given = [x for x in (file, path, url) if x]
    if len(given) != 1:
        raise HTTPException(400, "provide exactly one of: file, path, url")

    s = request.app.state.settings
    try:
        if file is not None:
            source = await _save_upload(file, s.uploads_dir, s.max_upload_mb)
        elif path:
            source = sources.from_path(path, s.allowed_roots, s.stable_seconds)
        else:
            source = sources.from_url(url or "")
    except sources.SourceError as exc:
        raise HTTPException(400, str(exc)) from exc

    engine = request.app.state.engine
    store = request.app.state.store

    existing = store.find_existing(source.info)
    if existing is not None:  # same bytes / same URL already processed or in progress -> no duplicate job
        if source.info.kind == "upload" and source.path:
            source.path.unlink(missing_ok=True)
        store.audit("api", "job.deduplicated", "job", existing.id, {"name": source.info.name})
        return {"job_id": existing.id, "state": existing.state, "deduplicated": True, "source": existing.source.model_dump()}

    job = store.create(source.info, engine.provider.name, engine.provider.model)
    if source.info.kind != "online":
        store.transition(job.id, JobState.STABLE, {"size_bytes": source.info.size_bytes})
    request.app.state.runner.submit(job.id, lambda on_stage: engine.analyze(job.id, source, on_stage))
    return {"job_id": job.id, "state": JobState.QUEUED, "deduplicated": False, "source": source.info.model_dump()}


@router.get("/jobs")
def list_jobs(request: Request) -> list[Job]:
    return request.app.state.store.list()


@router.get("/jobs/{job_id}")
def get_job(request: Request, job_id: str) -> Job:
    return _job_or_404(request, job_id)


@router.get("/jobs/{job_id}/result")
def get_result(request: Request, job_id: str) -> AnalysisResult:
    job = _job_or_404(request, job_id)
    result = request.app.state.store.load_result(job)
    if result is None:
        raise HTTPException(409, f"job {job_id} has no result yet (state={job.state})")
    return result


@router.get("/jobs/{job_id}/blocks")
def get_blocks(request: Request, job_id: str, block_type: str | None = None) -> list[Block]:
    job = _job_or_404(request, job_id)
    if job.state != JobState.BLOCKS_COMPLETE and not job.block_counts:
        raise HTTPException(409, f"job {job_id} has no blocks yet (state={job.state})")
    return request.app.state.store.blocks(job_id, block_type)


@router.get("/search")
def search(
    request: Request,
    q: str,
    block_type: str | None = None,
    media_id: str | None = None,
    mode: str = "hybrid",
    limit: int = 20,
) -> list[SearchHit]:
    """Search knowledge blocks. mode = hybrid (default: keyword + vector, rank-fused) | keyword | vector."""
    q = q.strip()
    if len(q) < 2:
        raise HTTPException(400, "q must be at least 2 characters")
    if mode not in ("hybrid", "keyword", "vector"):
        raise HTTPException(400, "mode must be hybrid, keyword or vector")
    store = request.app.state.store
    qvec = None
    if mode != "keyword" and store.supports_vectors:
        qvec = request.app.state.embedder.embed_query(q)
    return store.search(q, block_type, min(max(limit, 1), 100), query_vector=qvec, media_id=media_id, mode=mode)


def _job_or_404(request: Request, job_id: str) -> Job:
    job = request.app.state.store.get(job_id)
    if job is None:
        raise HTTPException(404, f"job {job_id} not found")
    return job


async def _save_upload(file: UploadFile, uploads_dir: Path, max_mb: int) -> sources.VideoSource:
    name = sources.safe_filename(file.filename or "video.mp4")
    if Path(name).suffix.lower() not in sources.VIDEO_EXTENSIONS:
        raise sources.SourceError(f"unsupported file type '{Path(name).suffix}'")
    import uuid

    dest = uploads_dir / f"{uuid.uuid4().hex[:8]}_{name}"
    limit = max_mb * 1024 * 1024
    written = 0
    with dest.open("wb") as out:
        while chunk := await file.read(CHUNK):
            written += len(chunk)
            if written > limit:
                out.close()
                dest.unlink(missing_ok=True)
                raise sources.SourceError(f"upload exceeds MAX_UPLOAD_MB={max_mb}")
            out.write(chunk)
    try:
        return sources.from_upload(dest)
    except sources.SourceError:
        dest.unlink(missing_ok=True)
        raise
