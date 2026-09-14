"""PostgreSQL store for the content layer: opportunities queue, drafts (+versions), agent runs."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

OpportunityStatus = Literal["new", "accepted", "dismissed", "drafted"]
DraftStatus = Literal["generating", "new", "in_review", "approved", "rejected", "failed"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _id() -> str:
    return uuid.uuid4().hex[:12]


class Opportunity(BaseModel):
    id: str
    job_id: str | None = None
    media_id: str | None = None
    block_id: str | None = None
    source: str = "ai"
    title: str
    reason: str | None = None
    suggested_formats: list[str] = Field(default_factory=list)
    audience: str | None = None
    confidence: float | None = None
    status: OpportunityStatus = "new"
    source_name: str | None = None
    created_at: datetime
    updated_at: datetime


class Draft(BaseModel):
    id: str
    opportunity_id: str | None = None
    kind: str = "blog"
    status: DraftStatus
    brief: str
    title: str | None = None
    slug: str | None = None
    body_markdown: str | None = None
    seo: dict[str, Any] = Field(default_factory=dict)
    social: dict[str, Any] = Field(default_factory=dict)
    citations: list[dict[str, Any]] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    hero_block_id: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    version: int = 1
    created_at: datetime
    updated_at: datetime

    @property
    def body_markdown_clean(self) -> str:
        from agents.blog_agent.agent import strip_citation_markers

        return strip_citation_markers(self.body_markdown or "")


_OPP_COLS = "o.*, m.name AS source_name"
_OPP_FROM = "FROM content_opportunities o LEFT JOIN media m ON m.id = o.media_id"


class ContentStore:
    def __init__(self, pool: ConnectionPool):
        self.pool = pool

    # ------------------------------------------------------------ opportunities
    def create_opportunities_from_job(self, job_id: str) -> int:
        """One queue row per content_opportunity block of the job (idempotent on block_id)."""
        with self.pool.connection() as conn, conn.transaction():
            rows = conn.execute(
                "SELECT id, media_id, payload FROM knowledge_blocks WHERE job_id = %s AND block_type = 'content_opportunity' ORDER BY ordinal",
                (job_id,),
            ).fetchall()
            n = 0
            for r in rows:
                p = r["payload"]
                cur = conn.execute(
                    """INSERT INTO content_opportunities (id, job_id, media_id, block_id, source, title, reason, suggested_formats, audience, confidence)
                       VALUES (%s, %s, %s, %s, 'ai', %s, %s, %s, %s, %s) ON CONFLICT (block_id) WHERE block_id IS NOT NULL DO NOTHING""",
                    (_id(), job_id, r["media_id"], r["id"], p.get("title") or "Untitled opportunity", p.get("reason"),
                     Jsonb(p.get("suggested_formats") or []), p.get("audience"), p.get("confidence")),
                )
                n += cur.rowcount
            return n

    def create_manual_opportunity(self, title: str, reason: str | None, formats: list[str], audience: str | None, job_id: str | None = None) -> Opportunity:
        media_id = None
        with self.pool.connection() as conn, conn.transaction():
            if job_id:
                r = conn.execute("SELECT media_id FROM processing_jobs WHERE id = %s", (job_id,)).fetchone()
                media_id = r["media_id"] if r else None
            oid = _id()
            conn.execute(
                """INSERT INTO content_opportunities (id, job_id, media_id, source, title, reason, suggested_formats, audience, confidence, status)
                   VALUES (%s, %s, %s, 'manual', %s, %s, %s, %s, NULL, 'accepted')""",
                (oid, job_id, media_id, title, reason, Jsonb(formats), audience),
            )
        return self.get_opportunity(oid)

    def list_opportunities(self, status: str | None = None, limit: int = 200) -> list[Opportunity]:
        with self.pool.connection() as conn:
            sql = f"SELECT {_OPP_COLS} {_OPP_FROM}"
            params: list[Any] = []
            if status:
                sql += " WHERE o.status = %s"; params.append(status)
            rows = conn.execute(sql + " ORDER BY o.created_at DESC LIMIT %s", [*params, limit]).fetchall()
            return [Opportunity.model_validate(r) for r in rows]

    def get_opportunity(self, oid: str) -> Opportunity | None:
        with self.pool.connection() as conn:
            r = conn.execute(f"SELECT {_OPP_COLS} {_OPP_FROM} WHERE o.id = %s", (oid,)).fetchone()
            return Opportunity.model_validate(r) if r else None

    def set_opportunity_status(self, oid: str, status: OpportunityStatus) -> Opportunity | None:
        with self.pool.connection() as conn, conn.transaction():
            conn.execute("UPDATE content_opportunities SET status = %s, updated_at = %s WHERE id = %s", (status, _now(), oid))
        return self.get_opportunity(oid)

    # ------------------------------------------------------------ drafts
    def create_draft(self, brief: str, opportunity_id: str | None, kind: str = "blog") -> Draft:
        did = _id()
        with self.pool.connection() as conn, conn.transaction():
            conn.execute("INSERT INTO drafts (id, opportunity_id, kind, status, brief) VALUES (%s, %s, %s, 'generating', %s)", (did, opportunity_id, kind, brief))
        return self.get_draft(did)

    def finish_draft(self, did: str, *, title: str, slug: str, body_markdown: str, seo: dict, social: dict, citations: list, evidence: dict,
                     hero_block_id: str | None, model: str, prompt_version: str, usage: dict, warnings: list[str]) -> Draft:
        with self.pool.connection() as conn, conn.transaction():
            conn.execute(
                """UPDATE drafts SET status = 'new', title = %s, slug = %s, body_markdown = %s, seo = %s, social = %s, citations = %s,
                          evidence = %s, hero_block_id = %s, model = %s, prompt_version = %s, usage = %s, warnings = %s, error = NULL, updated_at = %s
                   WHERE id = %s""",
                (title, slug, body_markdown, Jsonb(seo), Jsonb(social), Jsonb(citations), Jsonb(evidence), hero_block_id, model,
                 prompt_version, Jsonb(usage), Jsonb(warnings), _now(), did),
            )
            conn.execute(
                "INSERT INTO draft_versions (draft_id, version, title, body_markdown, edited_by) SELECT id, version, title, body_markdown, 'agent' FROM drafts WHERE id = %s",
                (did,),
            )
            r = conn.execute("SELECT opportunity_id FROM drafts WHERE id = %s", (did,)).fetchone()
            if r and r["opportunity_id"]:
                conn.execute("UPDATE content_opportunities SET status = 'drafted', updated_at = %s WHERE id = %s", (_now(), r["opportunity_id"]))
        return self.get_draft(did)

    def fail_draft(self, did: str, error: str) -> Draft:
        with self.pool.connection() as conn, conn.transaction():
            conn.execute("UPDATE drafts SET status = 'failed', error = %s, updated_at = %s WHERE id = %s", (error, _now(), did))
        return self.get_draft(did)

    def reset_draft_for_regeneration(self, did: str) -> Draft:
        with self.pool.connection() as conn, conn.transaction():
            conn.execute("UPDATE drafts SET status = 'generating', version = version + 1, error = NULL, updated_at = %s WHERE id = %s", (_now(), did))
        return self.get_draft(did)

    def update_draft_text(self, did: str, *, title: str | None, body_markdown: str | None, edited_by: str = "editor") -> Draft:
        """Editor change: previous text is kept in draft_versions, version bumps."""
        with self.pool.connection() as conn, conn.transaction():
            conn.execute(
                "INSERT INTO draft_versions (draft_id, version, title, body_markdown, edited_by) SELECT id, version, title, body_markdown, 'previous' FROM drafts WHERE id = %s",
                (did,),
            )
            conn.execute(
                """UPDATE drafts SET title = COALESCE(%s, title), body_markdown = COALESCE(%s, body_markdown), version = version + 1,
                          status = CASE WHEN status IN ('new', 'in_review') THEN 'in_review' ELSE status END, updated_at = %s WHERE id = %s""",
                (title, body_markdown, _now(), did),
            )
            conn.execute(
                "INSERT INTO draft_versions (draft_id, version, title, body_markdown, edited_by) SELECT id, version, title, body_markdown, %s FROM drafts WHERE id = %s",
                (edited_by, did),
            )
        return self.get_draft(did)

    def set_draft_status(self, did: str, status: DraftStatus) -> Draft:
        with self.pool.connection() as conn, conn.transaction():
            conn.execute("UPDATE drafts SET status = %s, updated_at = %s WHERE id = %s", (status, _now(), did))
        return self.get_draft(did)

    def get_draft(self, did: str) -> Draft | None:
        with self.pool.connection() as conn:
            r = conn.execute("SELECT * FROM drafts WHERE id = %s", (did,)).fetchone()
            return Draft.model_validate(r) if r else None

    def list_drafts(self, status: str | None = None, limit: int = 200) -> list[Draft]:
        with self.pool.connection() as conn:
            sql = "SELECT id, opportunity_id, kind, status, brief, title, slug, hero_block_id, model, prompt_version, usage, warnings, error, version, created_at, updated_at, '' AS body_markdown, '{}'::jsonb AS seo, '{}'::jsonb AS social, '[]'::jsonb AS citations, '{}'::jsonb AS evidence FROM drafts"
            params: list[Any] = []
            if status:
                sql += " WHERE status = %s"; params.append(status)
            rows = conn.execute(sql + " ORDER BY created_at DESC LIMIT %s", [*params, limit]).fetchall()
            return [Draft.model_validate(r) for r in rows]

    def draft_versions(self, did: str) -> list[dict[str, Any]]:
        with self.pool.connection() as conn:
            return conn.execute("SELECT version, title, edited_by, created_at, length(body_markdown) AS chars FROM draft_versions WHERE draft_id = %s ORDER BY id", (did,)).fetchall()

    # ------------------------------------------------------------ agent runs
    def record_run(self, agent: str, *, draft_id: str | None, opportunity_id: str | None, model: str | None, prompt_version: str | None,
                   status: str, usage: dict | None, seconds: float | None, error: str | None = None) -> None:
        with self.pool.connection() as conn, conn.transaction():
            conn.execute(
                "INSERT INTO agent_runs (id, agent, draft_id, opportunity_id, model, prompt_version, status, usage, seconds, error) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (_id(), agent, draft_id, opportunity_id, model, prompt_version, status, Jsonb(usage or {}), seconds, error),
            )

    def list_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.pool.connection() as conn:
            return conn.execute("SELECT * FROM agent_runs ORDER BY created_at DESC LIMIT %s", (limit,)).fetchall()
