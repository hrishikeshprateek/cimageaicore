"""Embed every knowledge block that has no vector yet:  python scripts/embed_backfill.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.api.config import get_settings  # noqa: E402
from apps.api.db import make_pool, run_migrations  # noqa: E402
from apps.api.jobs import embed_job_blocks  # noqa: E402
from apps.api.pg_store import PostgresJobStore  # noqa: E402
from services.ai_gateway.embeddings import build_embedder  # noqa: E402

s = get_settings()
if not s.database_url:
    sys.exit("DATABASE_URL is not set")
pool = make_pool(s.database_url)
run_migrations(pool)
store, embedder = PostgresJobStore(pool), build_embedder(s)
print("before:", store.embedding_stats(), "| embedder:", embedder.name, embedder.model, embedder.dimensions)
n = embed_job_blocks(store, embedder, None)
print("embedded:", n, "| after:", store.embedding_stats())
store.close()
