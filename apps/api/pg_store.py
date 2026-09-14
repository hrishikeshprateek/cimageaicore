"""PostgreSQL job store: media + processing_jobs + knowledge_blocks + audit_log.

Same interface as JsonJobStore. Blocks are flattened into rows at completion so
V0.3 can embed `text` into a pgvector column without touching the engine.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from apps.api.jobs import Job, JobState, StageEvent
from services.block_engine.media import ts_to_seconds
from services.block_engine.schemas import AnalysisResult, Block, SearchHit, SourceInfo, UsageInfo, flatten_blocks

log = logging.getLogger(__name__)

_JOB_COLS = """j.id, j.media_id, j.state, j.provider, j.model, j.stages, j.error, j.block_counts, j.usage,
               j.processing_seconds, j.warnings, j.created_at, j.updated_at,
               m.kind, m.name, m.path, m.url, m.mime_type, m.size_bytes, m.sha256, m.duration_seconds"""
_JOB_FROM = "FROM processing_jobs j JOIN media m ON m.id = j.media_id"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _row_to_job(r: dict[str, Any]) -> Job:
    return Job(
        id=r["id"],
        media_id=r["media_id"],
        created_at=r["created_at"],
        updated_at=r["updated_at"],
        state=JobState(r["state"]),
        source=SourceInfo(
            kind=r["kind"], name=r["name"], path=r["path"], url=r["url"], mime_type=r["mime_type"],
            size_bytes=r["size_bytes"], sha256=r["sha256"], duration_seconds=r["duration_seconds"],
        ),
        provider=r["provider"],
        model=r["model"],
        stages=[StageEvent.model_validate(s) for s in r["stages"]],
        error=r["error"],
        block_counts=r["block_counts"] or {},
        usage=UsageInfo.model_validate(r["usage"] or {}),
        processing_seconds=r["processing_seconds"],
        warnings=r["warnings"] or [],
    )


class PostgresJobStore:
    kind = "postgres"

    def __init__(self, pool: ConnectionPool):
        self.pool = pool

    # ---------------------------------------------------------------- lifecycle
    def load(self) -> int:
        """Jobs left active by a previous process cannot resume in V0.2 -> mark FAILED."""
        with self.pool.connection() as conn:
            active = [JobState(s) for s in JobState if s not in (JobState.BLOCKS_COMPLETE, JobState.INDEXED, JobState.CONTENT_CANDIDATE, JobState.FAILED)]
            rows = conn.execute("SELECT id FROM processing_jobs WHERE state = ANY(%s)", ([s.value for s in active],)).fetchall()
            for r in rows:
                self._transition(conn, r["id"], JobState.FAILED, {"reason": "interrupted by restart"}, error="interrupted by restart")
            return conn.execute("SELECT count(*) AS n FROM processing_jobs").fetchone()["n"]

    def close(self) -> None:
        self.pool.close()

    # ---------------------------------------------------------------- media
    def _upsert_media(self, conn, source: SourceInfo) -> str:
        if source.sha256:
            row = conn.execute("SELECT id FROM media WHERE sha256 = %s", (source.sha256,)).fetchone()
        elif source.url:
            row = conn.execute("SELECT id FROM media WHERE url = %s", (source.url,)).fetchone()
        else:
            row = None
        if row:
            return row["id"]
        media_id = uuid.uuid4().hex[:12]
        conn.execute(
            """INSERT INTO media (id, kind, name, path, url, mime_type, size_bytes, sha256, duration_seconds)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (media_id, source.kind, source.name, source.path, source.url, source.mime_type, source.size_bytes, source.sha256, source.duration_seconds),
        )
        return media_id

    def find_existing(self, source: SourceInfo) -> Job | None:
        with self.pool.connection() as conn:
            if source.sha256:
                cond, val = "m.sha256 = %s", source.sha256
            elif source.url:
                cond, val = "m.url = %s", source.url
            else:
                return None
            r = conn.execute(
                f"SELECT {_JOB_COLS} {_JOB_FROM} WHERE {cond} AND j.state <> 'FAILED' ORDER BY j.created_at DESC LIMIT 1", (val,)
            ).fetchone()
            return _row_to_job(r) if r else None

    # ---------------------------------------------------------------- jobs
    def create(self, source: SourceInfo, provider: str, model: str) -> Job:
        job = Job(source=source, provider=provider, model=model)
        job.stages.append(StageEvent(state=JobState.RECEIVED, detail={"name": source.name, "kind": source.kind}))
        with self.pool.connection() as conn, conn.transaction():
            job.media_id = self._upsert_media(conn, source)
            conn.execute(
                """INSERT INTO processing_jobs (id, media_id, state, provider, model, stages, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (job.id, job.media_id, job.state.value, provider, model, Jsonb([s.model_dump(mode="json") for s in job.stages]), job.created_at, job.updated_at),
            )
            self._audit(conn, "api", "job.created", "job", job.id, {"media_id": job.media_id, "kind": source.kind, "name": source.name})
        return job

    def get(self, job_id: str) -> Job | None:
        with self.pool.connection() as conn:
            r = conn.execute(f"SELECT {_JOB_COLS} {_JOB_FROM} WHERE j.id = %s", (job_id,)).fetchone()
            return _row_to_job(r) if r else None

    def list(self) -> list[Job]:
        with self.pool.connection() as conn:
            rows = conn.execute(f"SELECT {_JOB_COLS} {_JOB_FROM} ORDER BY j.created_at DESC LIMIT 500").fetchall()
            return [_row_to_job(r) for r in rows]

    def transition(self, job_id: str, state: JobState | str, detail: dict[str, Any] | None = None) -> Job:
        with self.pool.connection() as conn, conn.transaction():
            return self._transition(conn, job_id, JobState(state), detail or {})

    def _transition(self, conn, job_id: str, state: JobState, detail: dict[str, Any], *, error: str | None = None) -> Job:
        ev = StageEvent(state=state, detail=detail)
        conn.execute(
            """UPDATE processing_jobs
               SET state = %s, updated_at = %s, stages = stages || %s::jsonb, error = COALESCE(%s, error)
               WHERE id = %s""",
            (state.value, _now(), Jsonb([ev.model_dump(mode="json")]), error, job_id),
        )
        self._audit(conn, "worker", f"job.{state.value.lower()}", "job", job_id, detail)
        r = conn.execute(f"SELECT {_JOB_COLS} {_JOB_FROM} WHERE j.id = %s", (job_id,)).fetchone()
        return _row_to_job(r)

    def fail(self, job_id: str, error: str) -> Job:
        with self.pool.connection() as conn, conn.transaction():
            return self._transition(conn, job_id, JobState.FAILED, {"error": error}, error=error)

    def complete(self, job_id: str, result: AnalysisResult) -> Job:
        blocks = flatten_blocks(result)
        with self.pool.connection() as conn, conn.transaction():
            media_id = conn.execute("SELECT media_id FROM processing_jobs WHERE id = %s", (job_id,)).fetchone()["media_id"]
            conn.execute(
                """UPDATE processing_jobs
                   SET block_counts = %s, usage = %s, processing_seconds = %s, warnings = %s, repaired = %s,
                       prompt_version = %s, model = %s, result = %s, updated_at = %s
                   WHERE id = %s""",
                (Jsonb(result.block_counts), Jsonb(result.usage.model_dump()), result.processing_seconds, Jsonb(result.warnings),
                 result.repaired, result.prompt_version, result.model, Jsonb(result.model_dump(mode="json")), _now(), job_id),
            )
            conn.execute("DELETE FROM knowledge_blocks WHERE job_id = %s", (job_id,))  # idempotent re-run
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO knowledge_blocks
                       (id, job_id, media_id, block_type, ordinal, timestamp_ts, timestamp_seconds, text, payload, confidence, schema_version)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    [
                        (b.block_id, job_id, media_id, b.block_type, int(b.block_id.rsplit(":", 1)[1]), b.timestamp,
                         ts_to_seconds(b.timestamp) if b.timestamp else None, b.text, Jsonb(b.payload),
                         b.payload.get("confidence"), result.schema_version)
                        for b in blocks
                    ],
                )
            self._audit(conn, "worker", "blocks.stored", "job", job_id, {"blocks": len(blocks), "counts": result.block_counts})
            r = conn.execute(f"SELECT {_JOB_COLS} {_JOB_FROM} WHERE j.id = %s", (job_id,)).fetchone()
            return _row_to_job(r)

    def load_result(self, job: Job) -> AnalysisResult | None:
        with self.pool.connection() as conn:
            r = conn.execute("SELECT result FROM processing_jobs WHERE id = %s", (job.id,)).fetchone()
            return AnalysisResult.model_validate(r["result"]) if r and r["result"] else None

    # ---------------------------------------------------------------- blocks
    def blocks(self, job_id: str, block_type: str | None = None) -> list[Block]:
        with self.pool.connection() as conn:
            sql = "SELECT b.*, m.name AS source_name FROM knowledge_blocks b JOIN media m ON m.id = b.media_id WHERE b.job_id = %s"
            params: list[Any] = [job_id]
            if block_type:
                sql += " AND b.block_type = %s"
                params.append(block_type)
            rows = conn.execute(sql + " ORDER BY b.block_type, b.ordinal", params).fetchall()
            return [self._row_to_block(r) for r in rows]

    def search(self, q: str, block_type: str | None = None, limit: int = 20) -> list[SearchHit]:
        """Keyword search (Postgres full-text). V0.3 adds vector similarity for a hybrid ranking."""
        with self.pool.connection() as conn:
            sql = """SELECT b.*, m.name AS source_name,
                            ts_rank(to_tsvector('simple', b.text), plainto_tsquery('simple', %s)) AS rank
                     FROM knowledge_blocks b JOIN media m ON m.id = b.media_id
                     WHERE to_tsvector('simple', b.text) @@ plainto_tsquery('simple', %s)"""
            params: list[Any] = [q, q]
            if block_type:
                sql += " AND b.block_type = %s"
                params.append(block_type)
            rows = conn.execute(sql + " ORDER BY rank DESC, b.created_at DESC LIMIT %s", [*params, limit]).fetchall()
            return [SearchHit(**self._row_to_block(r).model_dump(), media_id=r["media_id"], rank=float(r["rank"])) for r in rows]

    @staticmethod
    def _row_to_block(r: dict[str, Any]) -> Block:
        return Block(block_id=r["id"], block_type=r["block_type"], job_id=r["job_id"], source_name=r["source_name"],
                     timestamp=r["timestamp_ts"], payload=r["payload"], text=r["text"])

    # ---------------------------------------------------------------- audit
    def audit(self, actor: str, action: str, entity_type: str | None = None, entity_id: str | None = None, detail: dict[str, Any] | None = None) -> None:
        with self.pool.connection() as conn:
            self._audit(conn, actor, action, entity_type, entity_id, detail or {})

    @staticmethod
    def _audit(conn, actor: str, action: str, entity_type: str | None, entity_id: str | None, detail: dict[str, Any]) -> None:
        conn.execute("INSERT INTO audit_log (actor, action, entity_type, entity_id, detail) VALUES (%s, %s, %s, %s, %s)",
                     (actor, action, entity_type, entity_id, Jsonb(detail)))
