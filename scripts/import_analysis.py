"""Backfill an AnalysisResult JSON (from data/analyses/) into PostgreSQL as a completed job.

    python scripts/import_analysis.py data/analyses/68ad4ad8f78a.json
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.api.config import get_settings  # noqa: E402
from apps.api.db import make_pool, run_migrations  # noqa: E402
from apps.api.jobs import JobState  # noqa: E402
from apps.api.pg_store import PostgresJobStore  # noqa: E402
from services.block_engine.schemas import AnalysisResult  # noqa: E402

s = get_settings()
if not s.database_url:
    sys.exit("DATABASE_URL is not set")
pool = make_pool(s.database_url)
run_migrations(pool)
store = PostgresJobStore(pool)
for arg in sys.argv[1:]:
    result = AnalysisResult.model_validate_json(Path(arg).read_text(encoding="utf-8"))
    if store.find_existing(result.source):
        print(f"skip {arg}: already stored")
        continue
    job = store.create(result.source, result.provider, result.model)
    for st in (JobState.STABLE, JobState.QUEUED, JobState.UPLOADED, JobState.ANALYZING):
        store.transition(job.id, st, {"imported_from": arg})
    result.job_id = job.id
    store.complete(job.id, result)
    store.transition(job.id, JobState.BLOCKS_COMPLETE, {"imported": True, "counts": result.block_counts})
    print(f"imported {arg} -> job {job.id} ({sum(result.block_counts.values()) + 2} blocks)")
store.close()
