"""Content layer API: opportunities queue, blog drafts, review states."""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from agents.blog_agent.agent import BlogAgent
from apps.api.content_store import ContentStore, Draft, Opportunity

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["content"])


def _content(request: Request) -> ContentStore:
    cs = getattr(request.app.state, "content", None)
    if cs is None:
        raise HTTPException(501, "content features need PostgreSQL (set DATABASE_URL)")
    return cs


# ------------------------------------------------------------------ opportunities
class ManualOpportunity(BaseModel):
    title: str = Field(min_length=3)
    reason: str | None = None
    suggested_formats: list[str] = Field(default_factory=lambda: ["blog", "linkedin", "instagram"])
    audience: str | None = None
    job_id: str | None = None


@router.get("/opportunities")
def list_opportunities(request: Request, status: str | None = None) -> list[Opportunity]:
    return _content(request).list_opportunities(status)


@router.post("/opportunities", status_code=status.HTTP_201_CREATED)
def create_opportunity(request: Request, body: ManualOpportunity) -> Opportunity:
    return _content(request).create_manual_opportunity(body.title, body.reason, body.suggested_formats, body.audience, body.job_id)


class StatusChange(BaseModel):
    status: Literal["new", "accepted", "dismissed"]


@router.post("/opportunities/{oid}/status")
def set_opportunity_status(request: Request, oid: str, body: StatusChange) -> Opportunity:
    opp = _content(request).set_opportunity_status(oid, body.status)
    if opp is None:
        raise HTTPException(404, "opportunity not found")
    return opp


# ------------------------------------------------------------------ drafts
class DraftRequest(BaseModel):
    opportunity_id: str | None = None
    brief: str | None = Field(default=None, description="Free-text brief when not drafting from an opportunity.")
    target_words: int = Field(default=1000, ge=200, le=3000)


def _run_agent(request: Request, draft_id: str, brief: str, opp: Opportunity | None, target_words: int) -> None:
    cs: ContentStore = request.app.state.content
    agent: BlogAgent = request.app.state.blog_agent

    def work() -> None:
        try:
            res = agent.draft(
                brief,
                job_id=opp.job_id if opp else None,
                extra_queries=[opp.reason] if opp and opp.reason else None,
                formats=opp.suggested_formats if opp else None,
                target_words=target_words,
            )
            d = res.draft
            cs.finish_draft(
                draft_id, title=d.title, slug=d.slug, body_markdown=d.body_markdown,
                seo={"seo_title": d.seo_title, "meta_description": d.meta_description, "excerpt": d.excerpt, "tags": d.tags, "evidence_gaps": d.evidence_gaps},
                social=d.social.model_dump(), citations=[c.model_dump() for c in d.citations], evidence=res.evidence.model_dump(),
                hero_block_id=d.hero_block_id, model=res.model, prompt_version=res.prompt_version, usage=res.usage, warnings=res.warnings,
            )
            cs.record_run("blog", draft_id=draft_id, opportunity_id=opp.id if opp else None, model=res.model, prompt_version=res.prompt_version,
                          status="ok", usage=res.usage, seconds=res.seconds)
            log.info("draft %s ready: %s (%d citations, %d warnings)", draft_id, d.title, len(d.citations), len(res.warnings))
        except Exception as exc:  # noqa: BLE001
            log.exception("draft %s failed", draft_id)
            cs.fail_draft(draft_id, f"{type(exc).__name__}: {exc}")
            cs.record_run("blog", draft_id=draft_id, opportunity_id=opp.id if opp else None, model=agent.model, prompt_version=agent.prompt_version,
                          status="failed", usage=None, seconds=None, error=str(exc)[:500])

    request.app.state.runner.run_async(work)


@router.post("/drafts", status_code=status.HTTP_202_ACCEPTED)
def create_draft(request: Request, body: DraftRequest) -> Draft:
    cs = _content(request)
    opp = None
    if body.opportunity_id:
        opp = cs.get_opportunity(body.opportunity_id)
        if opp is None:
            raise HTTPException(404, "opportunity not found")
        if opp.status == "dismissed":
            raise HTTPException(409, "opportunity was dismissed")
    brief = (body.brief or "").strip() or (opp.title if opp else "")
    if not brief:
        raise HTTPException(400, "give a brief or an opportunity_id")
    draft = cs.create_draft(brief, opp.id if opp else None)
    _run_agent(request, draft.id, brief, opp, body.target_words)
    return draft


@router.get("/drafts")
def list_drafts(request: Request, status: str | None = None) -> list[Draft]:
    return _content(request).list_drafts(status)


@router.get("/drafts/{did}")
def get_draft(request: Request, did: str) -> dict:
    d = _content(request).get_draft(did)
    if d is None:
        raise HTTPException(404, "draft not found")
    return {**d.model_dump(mode="json"), "body_markdown_clean": d.body_markdown_clean}


@router.get("/drafts/{did}/versions")
def draft_versions(request: Request, did: str) -> list[dict]:
    return _content(request).draft_versions(did)


@router.post("/drafts/{did}/regenerate", status_code=status.HTTP_202_ACCEPTED)
def regenerate_draft(request: Request, did: str, target_words: int = 1000) -> Draft:
    cs = _content(request)
    d = cs.get_draft(did)
    if d is None:
        raise HTTPException(404, "draft not found")
    if d.status == "generating":
        raise HTTPException(409, "already generating")
    opp = cs.get_opportunity(d.opportunity_id) if d.opportunity_id else None
    cs.reset_draft_for_regeneration(did)
    _run_agent(request, did, d.brief, opp, target_words)
    return cs.get_draft(did)


class DraftEdit(BaseModel):
    title: str | None = None
    body_markdown: str | None = None


@router.put("/drafts/{did}")
def edit_draft(request: Request, did: str, body: DraftEdit) -> Draft:
    cs = _content(request)
    d = cs.get_draft(did)
    if d is None:
        raise HTTPException(404, "draft not found")
    if d.status in ("generating", "failed"):
        raise HTTPException(409, f"draft is {d.status}")
    return cs.update_draft_text(did, title=body.title, body_markdown=body.body_markdown)


class DraftStatusChange(BaseModel):
    status: Literal["in_review", "approved", "rejected"]


@router.post("/drafts/{did}/status")
def set_draft_status(request: Request, did: str, body: DraftStatusChange) -> Draft:
    cs = _content(request)
    d = cs.get_draft(did)
    if d is None:
        raise HTTPException(404, "draft not found")
    if d.status in ("generating", "failed"):
        raise HTTPException(409, f"draft is {d.status}")
    request.app.state.store.audit("editor", f"draft.{body.status}", "draft", did, {"from": d.status})
    return cs.set_draft_status(did, body.status)


@router.get("/agent-runs")
def agent_runs(request: Request) -> list[dict]:
    return _content(request).list_runs()
