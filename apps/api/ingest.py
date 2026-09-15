"""One entry point for "analyse this source": the /analyze route, the folder watcher and scripts all go through here,
so dedupe, audit and the queue hand-off behave identically whichever way a video arrives."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from apps.api.jobs import Job, JobState
from services.block_engine.sources import VideoSource

log = logging.getLogger(__name__)


@dataclass
class Submission:
    job: Job
    deduplicated: bool

    @property
    def job_id(self) -> str:
        return self.job.id


def submit_source(state, source: VideoSource, *, actor: str = "api") -> Submission:
    """Create + queue a job for `source`, or return the existing job when the same bytes / URL were already processed."""
    store, engine, runner = state.store, state.engine, state.runner
    existing = store.find_existing(source.info)
    if existing is not None:  # same bytes / same URL already processed or in progress -> no duplicate job
        if source.info.kind == "upload" and source.path:
            source.path.unlink(missing_ok=True)
        store.audit(actor, "job.deduplicated", "job", existing.id, {"name": source.info.name, "path": source.info.path})
        return Submission(existing, True)
    job = store.create(source.info, engine.provider.name, engine.provider.model)
    if source.info.kind != "online":
        store.transition(job.id, JobState.STABLE, {"size_bytes": source.info.size_bytes, "via": actor})
    runner.submit(job.id, lambda on_stage: engine.analyze(job.id, source, on_stage))
    return Submission(job, False)
