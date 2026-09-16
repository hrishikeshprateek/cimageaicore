"""Prompt editor API: list every prompt kind with its versions, read/save versions (new files, never edits of bundled ones),
switch the active version and the institution context - applied live to the engine and the writer."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from services.prompts.registry import KINDS, PromptRegistry

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/prompts", tags=["prompts"])


def _reg(request: Request) -> PromptRegistry:
    reg = getattr(request.app.state, "prompts", None)
    if reg is None:
        raise HTTPException(503, "prompt registry not initialised")
    return reg


def _kind(key: str) -> str:
    if key not in KINDS:
        raise HTTPException(404, f"unknown prompt kind '{key}' (known: {', '.join(KINDS)})")
    return key


@router.get("")
def list_prompts(request: Request) -> dict[str, Any]:
    return _reg(request).describe()


@router.get("/{kind}/{version}")
def read_version(request: Request, kind: str, version: str) -> dict[str, Any]:
    reg = _reg(request); _kind(kind)
    try:
        text = reg.read(kind, version)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    src = "custom" if reg.path(kind, version) and str(reg.path(kind, version)).startswith(str(reg.overlay_dir)) else "bundled"
    return {"kind": kind, "version": version, "text": text, "source": src, "active": reg.active(kind) == version,
            "warnings": reg.warnings(kind, text), "suggested_version": reg.suggest_version(kind)}


class SaveBody(BaseModel):
    version: str = Field(min_length=1, max_length=64)
    text: str
    activate: bool = False
    overwrite: bool = False


@router.post("/{kind}")
def save_version(request: Request, kind: str, body: SaveBody) -> dict[str, Any]:
    reg = _reg(request); _kind(kind)
    try:
        path = reg.save(kind, body.version, body.text, overwrite=body.overwrite)
    except FileExistsError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    request.app.state.store.audit("admin", "prompt.saved", "prompt", f"{kind}/{body.version}", {"bytes": len(body.text), "overwrite": body.overwrite})
    if body.activate:
        reg.set_active(kind, body.version)
        request.app.state.store.audit("admin", "prompt.activated", "prompt", f"{kind}/{body.version}", {})
    elif reg.active(kind) == body.version:
        reg._notify()   # the active custom version was overwritten in place: reload it
    return {"saved": str(path), "active": reg.active(kind), "warnings": reg.warnings(kind, body.text), **reg.describe()}


class ActivateBody(BaseModel):
    version: str


@router.put("/{kind}/active")
def activate(request: Request, kind: str, body: ActivateBody) -> dict[str, Any]:
    reg = _reg(request); _kind(kind)
    try:
        reg.set_active(kind, body.version)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    request.app.state.store.audit("admin", "prompt.activated", "prompt", f"{kind}/{body.version}", {})
    return reg.describe()


@router.delete("/{kind}/{version}")
def delete_version(request: Request, kind: str, version: str) -> dict[str, Any]:
    reg = _reg(request); _kind(kind)
    try:
        reg.delete(kind, version)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    request.app.state.store.audit("admin", "prompt.deleted", "prompt", f"{kind}/{version}", {})
    return reg.describe()


class ContextBody(BaseModel):
    institution_context: str | None = None   # None or empty = back to the .env value


@router.put("/context")
def set_context(request: Request, body: ContextBody) -> dict[str, Any]:
    reg = _reg(request)
    reg.set_institution_context(body.institution_context)
    request.app.state.store.audit("admin", "prompt.context", "prompt", "institution_context", {"chars": len(body.institution_context or "")})
    return reg.describe()
