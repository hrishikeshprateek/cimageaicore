"""PostgreSQL store tests. Run against a throw-away database; skipped when no server is reachable."""
import os

import psycopg
import pytest

from apps.api.jobs import JobState
from services.ai_gateway.mock import MockProvider
from services.block_engine.engine import BlockEngine
from services.block_engine.sources import from_upload

ADMIN_URL = os.environ.get("TEST_DATABASE_ADMIN_URL", "postgresql://cimage:cimage@localhost:5433/cimage_ai")
TEST_DB = "cimage_ai_test"


@pytest.fixture(scope="module")
def test_db_url():
    try:
        with psycopg.connect(ADMIN_URL, connect_timeout=2, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
            conn.execute(f"CREATE DATABASE {TEST_DB}")
    except psycopg.Error as exc:
        pytest.skip(f"postgres not reachable: {exc}")
    yield ADMIN_URL.rsplit("/", 1)[0] + "/" + TEST_DB


@pytest.fixture
def store(test_db_url):
    from apps.api.db import make_pool, run_migrations
    from apps.api.pg_store import PostgresJobStore

    pool = make_pool(test_db_url)
    run_migrations(pool)
    st = PostgresJobStore(pool)
    yield st
    with pool.connection() as conn:
        conn.execute("TRUNCATE audit_log, knowledge_blocks, processing_jobs, media")
    st.close()


def _analyse(store, tiny_video, job):
    engine = BlockEngine(MockProvider(delay_seconds=0))
    result = engine.analyze(job.id, from_upload(tiny_video), lambda s, d: store.transition(job.id, s, d))
    store.complete(job.id, result)
    return store.transition(job.id, JobState.BLOCKS_COMPLETE, {"counts": result.block_counts})


def test_job_lifecycle_blocks_and_audit(store, tiny_video):
    src = from_upload(tiny_video)
    job = store.create(src.info, "mock", "mock-v1")
    assert store.get(job.id).state == JobState.RECEIVED and job.media_id
    done = _analyse(store, tiny_video, job)
    assert done.state == JobState.BLOCKS_COMPLETE
    assert [s.state for s in done.stages] == [JobState.RECEIVED, JobState.UPLOADED, JobState.ANALYZING, JobState.ANALYZING, JobState.BLOCKS_PARTIAL, JobState.BLOCKS_COMPLETE]
    blocks = store.blocks(job.id)
    assert len(blocks) == sum(done.block_counts.values()) + 2
    assert store.blocks(job.id, "quote")[0].block_type == "quote"
    assert store.load_result(done).analysis.video.title.startswith("[MOCK]")
    with store.pool.connection() as conn:
        actions = [r["action"] for r in conn.execute("SELECT action FROM audit_log WHERE entity_id = %s ORDER BY id", (job.id,))]
        assert actions[0] == "job.created" and actions[-2:] == ["blocks.stored", "job.blocks_complete"]
        assert conn.execute("SELECT count(*) AS n FROM media").fetchone()["n"] == 1


def test_dedupe_by_sha256_and_retry_after_failure(store, tiny_video):
    src = from_upload(tiny_video)
    job = store.create(src.info, "mock", "mock-v1")
    assert store.find_existing(src.info).id == job.id          # in progress -> reuse
    store.fail(job.id, "boom")
    assert store.find_existing(src.info) is None               # failed -> allowed to retry
    retry = store.create(src.info, "mock", "mock-v1")
    assert retry.media_id == job.media_id                      # same media row, new job
    with store.pool.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM media").fetchone()["n"] == 1


def test_keyword_search(store, tiny_video):
    job = store.create(from_upload(tiny_video).info, "mock", "mock-v1")
    _analyse(store, tiny_video, job)
    hits = store.search("placeholder quote")
    assert hits and hits[0].block_type == "quote" and hits[0].job_id == job.id
    assert store.search("placeholder", block_type="transcript")[0].block_type == "transcript"
    assert store.search("zzzznotthere") == []


def test_restart_marks_active_jobs_failed(store, tiny_video):
    job = store.create(from_upload(tiny_video).info, "mock", "mock-v1")
    store.transition(job.id, JobState.ANALYZING)
    store.load()
    assert store.get(job.id).state == JobState.FAILED and store.get(job.id).error == "interrupted by restart"
