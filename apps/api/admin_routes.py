"""Admin panel API: pipeline overview, folder-watcher control, audit trail. Serves web/admin.html's data."""
from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from apps.api.jobs import TERMINAL_STATES

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["admin"])

# Estimates only, for the dashboard's cost tile (paid-tier list prices, Sept 2026). The real bill is in Google Cloud billing.
PRICE_PER_M = {"input": 0.30, "output": 2.50}     # USD per 1M tokens, gemini-3.5-flash-lite
USD_INR = 88.0


def _watcher(request: Request):
    w = getattr(request.app.state, "watcher", None)
    if w is None:
        raise HTTPException(503, "watcher not initialised")
    return w


# ------------------------------------------------------------------ watcher
@router.get("/watcher")
def watcher_status(request: Request) -> dict[str, Any]:
    return _watcher(request).status().__dict__


@router.post("/watcher/scan")
def watcher_scan(request: Request, sync: bool = False) -> dict[str, Any]:
    """Scan now. `sync=true` runs the cycle inline and returns its counts (used by tests and scripts)."""
    w = _watcher(request)
    if sync:
        return {"ran": True, **w.scan_once()}
    w.trigger()
    return {"ran": False, "triggered": True}


@router.post("/watcher/pause")
def watcher_pause(request: Request) -> dict[str, Any]:
    w = _watcher(request)
    w.set_paused(True)
    request.app.state.store.audit("admin", "watcher.paused")
    return w.status().__dict__


@router.post("/watcher/resume")
def watcher_resume(request: Request) -> dict[str, Any]:
    w = _watcher(request)
    w.set_paused(False)
    request.app.state.store.audit("admin", "watcher.resumed")
    w.trigger()
    return w.status().__dict__


@router.get("/watcher/index")
def watcher_index(request: Request) -> list[dict[str, Any]]:
    idx = _watcher(request).index()
    return [{"path": p, **v} for p, v in sorted(idx.items(), key=lambda kv: kv[1].get("at") or "", reverse=True)]


class ForgetBody(BaseModel):
    path: str


@router.post("/watcher/forget")
def watcher_forget(request: Request, body: ForgetBody) -> dict[str, Any]:
    """Drop a file from the watcher's index so the next scan picks it up again (e.g. after it was re-exported)."""
    ok = _watcher(request).forget(body.path)
    if ok:
        request.app.state.store.audit("admin", "watcher.forget", "file", Path(body.path).name, {"path": body.path})
        _watcher(request).trigger()
    return {"forgotten": ok}


# ------------------------------------------------------------------ overview
@router.get("/admin/overview")
def overview(request: Request) -> dict[str, Any]:
    st = request.app.state
    store, settings = st.store, st.settings
    jobs = store.list()
    by_state = Counter(j.state.value for j in jobs)
    active = [j for j in jobs if j.state not in TERMINAL_STATES]
    tok_in = sum(j.usage.input_tokens or 0 for j in jobs)
    tok_out = sum((j.usage.output_tokens or 0) + (j.usage.thought_tokens or 0) for j in jobs)
    usd = tok_in * PRICE_PER_M["input"] / 1e6 + tok_out * PRICE_PER_M["output"] / 1e6
    minutes = sum((j.source.duration_seconds or 0) for j in jobs) / 60

    pg = getattr(store, "supports_vectors", False)
    blocks = store.block_count() if pg else sum(sum(j.block_counts.values()) for j in jobs)
    embeddings = store.embedding_stats(st.embedder.model) if pg else {"total": 0, "embedded": 0, "pending": 0, "stale": 0, "by_model": {}}

    content: dict[str, Any] = {"available": pg, "opportunities": {}, "drafts": {}}
    if pg and getattr(st, "content", None):
        cs = st.content
        content["opportunities"] = dict(Counter(o.status for o in cs.list_opportunities(limit=1000)))
        drafts = cs.list_drafts(limit=1000)
        content["drafts"] = dict(Counter(d.status for d in drafts))
        content["awaiting_review"] = sum(1 for d in drafts if d.status in ("new", "in_review"))
        content["recent_runs"] = [
            {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items() if k in ("id", "agent", "draft_id", "status", "model", "seconds", "created_at", "error")}
            for r in cs.list_runs(limit=8)
        ]

    watcher = st.watcher.status().__dict__ if getattr(st, "watcher", None) else None
    audit = [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()} for r in store.recent_audit(25)] if pg else []

    return {
        "system": {
            "version": settings.app_version, "provider": st.engine.provider.name, "model": st.engine.provider.model,
            "embedder": st.embedder.name, "embedding_model": st.embedder.model, "store": store.kind,
            "auto_draft": settings.auto_draft, "watcher_enabled": settings.watcher_enabled,
        },
        "jobs": {"total": len(jobs), "active": len(active), "by_state": dict(by_state),
                 "minutes_of_video": round(minutes, 1),
                 "recent": [{"id": j.id, "name": j.source.name, "kind": j.source.kind, "state": j.state.value, "created_at": j.created_at.isoformat(),
                             "duration_seconds": j.source.duration_seconds, "blocks": sum(j.block_counts.values())} for j in jobs[:8]]},
        "blocks": {"total": blocks, "embeddings": embeddings},
        "cost": {"input_tokens": tok_in, "output_tokens": tok_out, "usd": round(usd, 4), "inr": round(usd * USD_INR, 2), "note": "estimate at paid-tier list prices"},
        "content": content,
        "watcher": watcher,
        "audit": audit,
    }


@router.get("/audit")
def audit(request: Request, limit: int = 50, actor: str | None = None) -> list[dict[str, Any]]:
    store = request.app.state.store
    if not getattr(store, "supports_vectors", False):
        return []
    return [{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()} for r in store.recent_audit(min(max(limit, 1), 500), actor=actor)]
