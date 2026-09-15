"""Import jobs recorded by the JSON-file store (data/jobs/*.json, before DATABASE_URL was set) into PostgreSQL.

Original job ids and timestamps are kept so history stays chronological. A completed
job gets its AnalysisResult stored on the row (the UI detail view reads that); its
knowledge_blocks rows are inserted only if the media has no blocks yet, so a video
analysed twice doesn't return every quote twice in search.

    python scripts/import_json_jobs.py                       # every data/jobs/*.json
    python scripts/import_json_jobs.py data/jobs/68ad4ad8f78a.json
"""
import sys
from pathlib import Path

from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.api.config import REPO_ROOT, get_settings  # noqa: E402
from apps.api.db import make_pool, run_migrations  # noqa: E402
from apps.api.jobs import Job, StageEvent  # noqa: E402
from apps.api.pg_store import PostgresJobStore  # noqa: E402
from services.block_engine.schemas import AnalysisResult  # noqa: E402

s = get_settings()
if not s.database_url:
    sys.exit("DATABASE_URL is not set")
pool = make_pool(s.database_url)
run_migrations(pool)
store = PostgresJobStore(pool)

paths = [Path(a) for a in sys.argv[1:]] or sorted(s.jobs_dir.glob("*.json"))
for p in paths:
    job = Job.model_validate_json(p.read_text(encoding="utf-8"))
    rel = str(p.resolve().relative_to(REPO_ROOT)) if p.resolve().is_relative_to(REPO_ROOT) else str(p)
    with pool.connection() as conn, conn.transaction():
        if conn.execute("SELECT 1 FROM processing_jobs WHERE id = %s", (job.id,)).fetchone():
            print(f"skip {job.id}: already in Postgres")
            continue
        media_id = store._upsert_media(conn, job.source)
        stages = [st.model_dump(mode="json") for st in job.stages]
        stages.append(StageEvent(state=job.state, detail={"imported_from": rel}).model_dump(mode="json"))
        conn.execute(
            """INSERT INTO processing_jobs
               (id, media_id, state, provider, model, stages, error, block_counts, usage, processing_seconds, warnings, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (job.id, media_id, job.state.value, job.provider, job.model, Jsonb(stages), job.error, Jsonb(job.block_counts),
             Jsonb(job.usage.model_dump()), job.processing_seconds, Jsonb(job.warnings), job.created_at, job.updated_at),
        )
        store._audit(conn, "import", "job.imported", "job", job.id, {"from": rel, "media_id": media_id, "state": job.state.value})
        media_has_blocks = conn.execute("SELECT 1 FROM knowledge_blocks WHERE media_id = %s LIMIT 1", (media_id,)).fetchone() is not None

    if not job.result_path:
        print(f"imported {job.id} {job.state.value:16} {job.source.name} (no result)")
        continue
    rp = Path(job.result_path)
    rp = rp if rp.is_absolute() else REPO_ROOT / rp
    if not rp.exists():
        print(f"imported {job.id} {job.state.value:16} {job.source.name} (result file missing: {job.result_path})")
        continue
    result = AnalysisResult.model_validate_json(rp.read_text(encoding="utf-8"))
    result.job_id = job.id
    if media_has_blocks:
        with pool.connection() as conn, conn.transaction():
            conn.execute(
                "UPDATE processing_jobs SET result = %s, repaired = %s, prompt_version = %s WHERE id = %s",
                (Jsonb(result.model_dump(mode="json")), result.repaired, result.prompt_version, job.id),
            )
        note = "result stored; blocks skipped (media already indexed)"
    else:
        store.complete(job.id, result)
        note = f"result + {sum(result.block_counts.values())} blocks stored (run scripts/embed_backfill.py to embed)"
    with pool.connection() as conn:  # complete() bumps updated_at; keep the original timestamp
        conn.execute("UPDATE processing_jobs SET updated_at = %s WHERE id = %s", (job.updated_at, job.id))
    print(f"imported {job.id} {job.state.value:16} {job.source.name} — {note}")
store.close()
