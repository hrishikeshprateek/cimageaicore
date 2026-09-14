"""Backfill everything derivable from stored jobs: embeddings, then the opportunities queue.  python scripts/backfill.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.api.config import get_settings  # noqa: E402
from apps.api.content_store import ContentStore  # noqa: E402
from apps.api.db import make_pool, run_migrations  # noqa: E402
from apps.api.jobs import JobState, embed_job_blocks  # noqa: E402
from apps.api.pg_store import PostgresJobStore  # noqa: E402
from services.ai_gateway.embeddings import build_embedder  # noqa: E402

s = get_settings()
pool = make_pool(s.database_url)
run_migrations(pool)
store, content = PostgresJobStore(pool), ContentStore(pool)
n = embed_job_blocks(store, build_embedder(s), None)
print("embedded:", n, store.embedding_stats())
created = 0
for job in store.list():
    if job.state in (JobState.BLOCKS_COMPLETE, JobState.INDEXED, JobState.CONTENT_CANDIDATE):
        c = content.create_opportunities_from_job(job.id)
        if c and job.state != JobState.CONTENT_CANDIDATE:
            store.transition(job.id, JobState.CONTENT_CANDIDATE, {"opportunities": c, "backfill": True})
        created += c
print("opportunities created:", created, "| queue:", len(content.list_opportunities("new")))
store.close()
