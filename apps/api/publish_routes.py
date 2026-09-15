"""Publishing API: WordPress targets (configured in the UI, secrets encrypted at rest) and per-draft publications.
Approval in content_routes calls `auto_publish()`; nothing is ever published without a human approval."""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, HttpUrl

from services.publishing.crypto import SecretBox
from services.publishing.publisher import PublishError, publish_draft, test_target
from services.publishing.store import Publication, PublishStore, Target, TargetLayout

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["publishing"])


def publish_store(request: Request) -> PublishStore:
    st = getattr(request.app.state, "publish", None)
    if st is None:
        store = request.app.state.store
        if not getattr(store, "supports_vectors", False):
            raise HTTPException(501, "publishing needs PostgreSQL (set DATABASE_URL)")
        settings = request.app.state.settings
        st = PublishStore(store.pool, SecretBox(key_file=settings.data_dir / ".secret_key"))
        request.app.state.publish = st
    return st


# ------------------------------------------------------------------ targets
class TargetIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    url: HttpUrl
    username: str = Field(min_length=1)
    app_password: str | None = Field(default=None, description="WordPress application password (Users → Profile → Application Passwords). Write-only.")
    mode: Literal["draft", "publish"] = "draft"
    auto_on_approval: bool = True
    enabled: bool = True
    default_category: str | None = None
    seo_plugin: Literal["none", "yoast", "rankmath"] = "none"
    layout: TargetLayout = Field(default_factory=TargetLayout)


class TargetPatch(BaseModel):
    name: str | None = None
    url: HttpUrl | None = None
    username: str | None = None
    app_password: str | None = None
    mode: Literal["draft", "publish"] | None = None
    auto_on_approval: bool | None = None
    enabled: bool | None = None
    default_category: str | None = None
    seo_plugin: Literal["none", "yoast", "rankmath"] | None = None
    layout: TargetLayout | None = None


@router.get("/publish/targets")
def list_targets(request: Request) -> list[Target]:
    return publish_store(request).list_targets()


@router.post("/publish/targets", status_code=status.HTTP_201_CREATED)
def create_target(request: Request, body: TargetIn) -> Target:
    if not body.app_password or not body.app_password.strip():
        raise HTTPException(400, "app_password is required when adding a site")
    st = publish_store(request)
    t = Target(name=body.name.strip(), url=str(body.url).rstrip("/"), username=body.username.strip(), mode=body.mode, auto_on_approval=body.auto_on_approval,
               enabled=body.enabled, default_category=(body.default_category or "").strip() or None, seo_plugin=body.seo_plugin, layout=body.layout)
    t = st.create_target(t, body.app_password.replace(" ", "") if len(body.app_password.replace(" ", "")) == 24 else body.app_password.strip())
    request.app.state.store.audit("editor", "publish_target.created", "publish_target", t.id, {"name": t.name, "url": t.url, "mode": t.mode})
    return t


@router.put("/publish/targets/{target_id}")
def update_target(request: Request, target_id: str, body: TargetPatch) -> Target:
    st = publish_store(request)
    if st.get_target(target_id) is None:
        raise HTTPException(404, "target not found")
    fields = {k: v for k, v in body.model_dump().items() if v is not None and k != "app_password"}
    if body.layout is not None:
        fields["layout"] = body.layout
    if "url" in fields:
        fields["url"] = str(fields["url"]).rstrip("/")
    if "default_category" in fields:
        fields["default_category"] = fields["default_category"].strip() or None
    secret = body.app_password.strip() if body.app_password and body.app_password.strip() else None
    if secret and len(secret.replace(" ", "")) == 24:
        secret = secret.replace(" ", "")
    t = st.update_target(target_id, secret=secret, **fields)
    request.app.state.store.audit("editor", "publish_target.updated", "publish_target", target_id,
                                  body.model_dump(mode="json", exclude_none=True, exclude={"app_password"}) | ({"secret": "rotated"} if secret else {}))
    return t


@router.delete("/publish/targets/{target_id}")
def delete_target(request: Request, target_id: str) -> dict:
    st = publish_store(request)
    if not st.delete_target(target_id):
        raise HTTPException(404, "target not found")
    request.app.state.store.audit("editor", "publish_target.deleted", "publish_target", target_id, {})
    return {"deleted": target_id}


@router.post("/publish/targets/{target_id}/test")
def test_connection(request: Request, target_id: str) -> dict:
    st = publish_store(request)
    t = st.get_target(target_id)
    if t is None:
        raise HTTPException(404, "target not found")
    try:
        info = test_target(st, t)
    except PublishError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"ok": True, **info}


# ------------------------------------------------------------------ publications
class PublishRequest(BaseModel):
    target_ids: list[str] = Field(default_factory=list, description="Empty = every enabled target.")
    mode: Literal["draft", "publish"] | None = Field(default=None, description="Override the target's mode for this run.")


def _run_publications(request: Request, draft, targets: list[Target], mode: str | None, triggered_by: str) -> list[Publication]:
    st = publish_store(request)
    images = getattr(request.app.state, "images", None)
    store = request.app.state.store
    queued = [st.upsert_publication(draft.id, t.id, mode=mode or t.mode, triggered_by=triggered_by) for t in targets]

    def work() -> None:
        for t in targets:
            pub = publish_draft(draft, t, st, images, mode=mode, triggered_by=triggered_by)
            store.audit("publisher", f"publication.{pub.status}", "draft", draft.id,
                        {"target": t.name, "mode": pub.mode, "remote_id": pub.remote_id, "url": pub.remote_url, "error": pub.error, "by": triggered_by})
            log.info("draft %s -> %s: %s %s", draft.id, t.name, pub.status, pub.remote_url or pub.error or "")

    request.app.state.runner.run_async(work)
    return queued


@router.post("/drafts/{did}/publish", status_code=status.HTTP_202_ACCEPTED)
def publish_now(request: Request, did: str, body: PublishRequest) -> list[Publication]:
    """Editor-triggered publish (or re-publish) of an APPROVED draft to chosen targets."""
    cs = request.app.state.content
    if cs is None:
        raise HTTPException(501, "content features need PostgreSQL")
    d = cs.get_draft(did)
    if d is None:
        raise HTTPException(404, "draft not found")
    if d.status != "approved":
        raise HTTPException(409, f"only approved drafts can be published (this one is {d.status})")
    st = publish_store(request)
    targets = st.list_targets()
    if body.target_ids:
        chosen = [t for t in targets if t.id in body.target_ids]
        if len(chosen) != len(set(body.target_ids)):
            raise HTTPException(404, "unknown target id")
    else:
        chosen = [t for t in targets if t.enabled]
    if not chosen:
        raise HTTPException(400, "no publishing targets configured - add a website first")
    return _run_publications(request, d, chosen, body.mode, "editor")


def auto_publish(request: Request, draft) -> list[Publication]:
    """Called on approval: every enabled target with auto_on_approval, in its own mode. Quiet when publishing is not configured."""
    try:
        st = publish_store(request)
    except HTTPException:
        return []
    targets = [t for t in st.list_targets(enabled_only=True) if t.auto_on_approval]
    if not targets:
        return []
    return _run_publications(request, draft, targets, None, "approval")


@router.get("/drafts/{did}/publications")
def draft_publications(request: Request, did: str) -> list[Publication]:
    return publish_store(request).list_publications(draft_id=did)


@router.get("/publish/publications")
def all_publications(request: Request, limit: int = 100) -> list[Publication]:
    return publish_store(request).list_publications(limit=min(max(limit, 1), 500))
