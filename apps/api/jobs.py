"""Jobs: progressive states, a JSON-file-backed store and an in-process runner.

Phase 3 replaces the store with PostgreSQL and the runner with Redis + workers;
the state machine and the `Job` shape stay the same.
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

from services.block_engine.schemas import AnalysisResult, Block, SearchHit, SourceInfo, UsageInfo, flatten_blocks

log = logging.getLogger(__name__)


class JobState(str, Enum):
    RECEIVED = "RECEIVED"
    STABLE = "STABLE"
    QUEUED = "QUEUED"
    UPLOADED = "UPLOADED"
    ANALYZING = "ANALYZING"
    BLOCKS_PARTIAL = "BLOCKS_PARTIAL"
    BLOCKS_COMPLETE = "BLOCKS_COMPLETE"
    EMBEDDING = "EMBEDDING"          # Phase 3
    INDEXED = "INDEXED"              # Phase 3
    CONTENT_CANDIDATE = "CONTENT_CANDIDATE"  # Phase 5
    FAILED = "FAILED"


TERMINAL_STATES = {JobState.BLOCKS_COMPLETE, JobState.INDEXED, JobState.CONTENT_CANDIDATE, JobState.FAILED}


def _now() -> datetime:
    return datetime.now(timezone.utc)


class StageEvent(BaseModel):
    state: JobState
    at: datetime = Field(default_factory=_now)
    detail: dict[str, Any] = Field(default_factory=dict)


class Job(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    state: JobState = JobState.RECEIVED
    media_id: str | None = None
    source: SourceInfo
    provider: str
    model: str
    stages: list[StageEvent] = Field(default_factory=list)
    error: str | None = None
    result_path: str | None = None
    block_counts: dict[str, int] = Field(default_factory=dict)
    usage: UsageInfo = Field(default_factory=UsageInfo)
    processing_seconds: float | None = None
    warnings: list[str] = Field(default_factory=list)

    @property
    def is_active(self) -> bool:
        return self.state not in TERMINAL_STATES


class JsonJobStore:
    """Thread-safe in-memory map persisted as one JSON file per job (no database needed).

    Implements the same interface as PostgresJobStore (apps/api/pg_store.py).
    """

    kind = "json"
    supports_vectors = False

    def __init__(self, jobs_dir: Path, analyses_dir: Path):
        self.jobs_dir = jobs_dir
        self.analyses_dir = analyses_dir
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()

    def load(self) -> int:
        with self._lock:
            for f in sorted(self.jobs_dir.glob("*.json")):
                try:
                    job = Job.model_validate_json(f.read_text(encoding="utf-8"))
                except Exception as exc:  # noqa: BLE001
                    log.warning("skipping unreadable job file %s: %s", f.name, exc)
                    continue
                if job.is_active:  # process died mid-run; nothing will resume it in V0.1
                    job.state = JobState.FAILED
                    job.error = "interrupted by restart"
                    job.stages.append(StageEvent(state=JobState.FAILED, detail={"reason": job.error}))
                    self._persist(job)
                self._jobs[job.id] = job
            return len(self._jobs)

    def create(self, source: SourceInfo, provider: str, model: str) -> Job:
        job = Job(source=source, provider=provider, model=model, media_id=source.sha256 or source.url)
        job.stages.append(StageEvent(state=JobState.RECEIVED, detail={"name": source.name, "kind": source.kind}))
        with self._lock:
            self._jobs[job.id] = job
            self._persist(job)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def transition(self, job_id: str, state: JobState | str, detail: dict[str, Any] | None = None) -> Job:
        state = JobState(state)
        with self._lock:
            job = self._jobs[job_id]
            job.state = state
            job.updated_at = _now()
            job.stages.append(StageEvent(state=state, detail=detail or {}))
            self._persist(job)
            return job

    def complete(self, job_id: str, result: AnalysisResult) -> Job:
        path = self.analyses_dir / f"{job_id}.json"
        path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        with self._lock:
            job = self._jobs[job_id]
            job.result_path = str(path)
            job.block_counts = result.block_counts
            job.usage = result.usage
            job.processing_seconds = result.processing_seconds
            job.warnings = result.warnings
            job.updated_at = _now()
            self._persist(job)
            return job

    def fail(self, job_id: str, error: str) -> Job:
        with self._lock:
            job = self._jobs[job_id]
            job.error = error
            return self.transition(job_id, JobState.FAILED, {"error": error})

    def load_result(self, job: Job) -> AnalysisResult | None:
        if not job.result_path:
            return None
        return AnalysisResult.model_validate_json(Path(job.result_path).read_text(encoding="utf-8"))

    def blocks(self, job_id: str, block_type: str | None = None) -> list[Block]:
        job = self.get(job_id)
        result = self.load_result(job) if job else None
        if result is None:
            return []
        rows = flatten_blocks(result)
        return [b for b in rows if b.block_type == block_type] if block_type else rows

    def find_existing(self, source: SourceInfo) -> Job | None:
        """Same content already processed (or in progress)? Return that job instead of starting another."""
        for job in self.list():
            same = (source.sha256 and job.source.sha256 == source.sha256) or (source.url and job.source.url == source.url)
            if same and job.state != JobState.FAILED:
                return job
        return None

    def search(self, q: str, block_type: str | None = None, limit: int = 20, *, query_vector=None, media_id: str | None = None, mode: str = "hybrid") -> list[SearchHit]:
        needle = q.lower().strip()
        hits: list[SearchHit] = []
        for job in self.list():
            if media_id and job.media_id != media_id:
                continue
            for b in self.blocks(job.id, block_type):
                if needle and needle in b.text.lower():
                    hits.append(SearchHit(**b.model_dump(), media_id=job.media_id, rank=1.0, matched_by=["keyword"]))
        return hits[:limit]

    def pending_embeddings(self, job_id: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        return []

    def set_embeddings(self, rows, model: str) -> int:
        return 0

    def embedding_stats(self) -> dict[str, int]:
        return {"total": 0, "embedded": 0, "pending": 0}

    def audit(self, actor: str, action: str, entity_type: str | None = None, entity_id: str | None = None, detail: dict[str, Any] | None = None) -> None:
        log.info("audit %s %s %s/%s %s", actor, action, entity_type, entity_id, detail or {})

    def close(self) -> None:
        pass

    def _persist(self, job: Job) -> None:
        (self.jobs_dir / f"{job.id}.json").write_text(job.model_dump_json(indent=2), encoding="utf-8")


def embed_job_blocks(store, embedder, job_id: str) -> int:
    """Embed every block of a job that has no vector yet. Returns the number embedded."""
    total = 0
    while True:
        pending = store.pending_embeddings(job_id, limit=200)
        if not pending:
            return total
        vectors = embedder.embed_documents([(r["title"], r["text"]) for r in pending])
        total += store.set_embeddings([(r["id"], v) for r, v in zip(pending, vectors)], embedder.model)


class JobRunner:
    """Tiny in-process queue (thread pool). Stands in for Redis + workers until Phase 2."""

    def __init__(self, store, worker_threads: int = 2, embedder=None, content_store=None):
        self.store = store
        self.embedder = embedder
        self.content_store = content_store
        self.pool = ThreadPoolExecutor(max_workers=worker_threads, thread_name_prefix="video-worker")

    def run_async(self, fn: Callable[[], None]) -> None:
        """Run any background task (e.g. a blog draft) on the same pool."""
        self.pool.submit(fn)

    def submit(self, job_id: str, work: Callable[[Callable[[str, dict[str, Any]], None]], AnalysisResult]) -> None:
        self.store.transition(job_id, JobState.QUEUED)

        def on_stage(state: str, detail: dict[str, Any]) -> None:
            self.store.transition(job_id, state, detail)

        def run() -> None:
            try:
                result = work(on_stage)
                self.store.complete(job_id, result)
                self.store.transition(job_id, JobState.BLOCKS_COMPLETE, {"repaired": result.repaired, "counts": result.block_counts})
                log.info("job %s complete: %s", job_id, result.block_counts)
            except Exception as exc:  # noqa: BLE001 - any failure must land in the job record
                log.exception("job %s failed", job_id)
                self.store.fail(job_id, f"{type(exc).__name__}: {exc}")
                return
            if self.embedder is None or not getattr(self.store, "supports_vectors", False):
                return
            try:  # blocks are safe already; an embedding failure is recoverable via scripts/embed_backfill.py
                self.store.transition(job_id, JobState.EMBEDDING, {"model": self.embedder.model})
                n = embed_job_blocks(self.store, self.embedder, job_id)
                self.store.transition(job_id, JobState.INDEXED, {"embedded": n, "model": self.embedder.model})
            except Exception as exc:  # noqa: BLE001
                log.exception("job %s embedding failed", job_id)
                self.store.transition(job_id, JobState.BLOCKS_COMPLETE, {"embedding_error": f"{type(exc).__name__}: {exc}"})
                return
            if self.content_store is not None:
                try:
                    created = self.content_store.create_opportunities_from_job(job_id)
                    if created:
                        self.store.transition(job_id, JobState.CONTENT_CANDIDATE, {"opportunities": created})
                except Exception as exc:  # noqa: BLE001
                    log.exception("job %s opportunity extraction failed", job_id)

        self.pool.submit(run)

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
