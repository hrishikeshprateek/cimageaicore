"""NAS / watched-folder picker: browse mounted locations, choose which folders the watcher scans, applied live."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from services.ingestion.config import WatcherConfig, folder_status, is_browsable, list_dir, load_config, places, save_config

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/nas", tags=["nas"])


def _watcher(request: Request):
    w = getattr(request.app.state, "watcher", None)
    if w is None:
        raise HTTPException(503, "watcher not initialised")
    return w


def _env_roots(request: Request) -> list[Path]:
    return list(request.app.state.settings.watch_roots_resolved)


def _browsable_bases(request: Request) -> list[Path]:
    s = request.app.state.settings
    w = _watcher(request)
    return places(list(s.allowed_roots) + list(w.roots) + _env_roots(request))


@router.get("/roots")
def roots(request: Request) -> dict[str, Any]:
    """Watched folders with their status; `source` says whether a folder comes from .env (fixed) or was picked in the UI."""
    s = request.app.state.settings
    w = _watcher(request)
    env = [str(r) for r in _env_roots(request)]
    out = []
    for r in w.roots:
        st = folder_status(Path(r))
        st["source"] = "env" if str(r) in env else "ui"
        out.append(st)
    in_docker = Path("/.dockerenv").exists()
    return {
        "roots": out,
        "places": [folder_status(p) for p in _browsable_bases(request)],
        "docker": in_docker,
        "hint": ("Inside Docker the NAS folder from NAS_WATCH_DIR appears as /nas. Other folders must be mounted into the container to be visible here."
                 if in_docker else "Connect the NAS in Finder / mount it on this machine, then pick the folder here."),
        "config_file": str(s.watcher_config_file),
        "enabled": w.enabled, "running": w.status().running,
    }


class RootsBody(BaseModel):
    roots: list[str] = Field(description="UI-managed watched folders (absolute paths). Folders from .env are always kept.")


@router.put("/roots")
def set_roots(request: Request, body: RootsBody) -> dict[str, Any]:
    s = request.app.state.settings
    w = _watcher(request)
    bases = _browsable_bases(request)
    chosen: list[Path] = []
    for raw in body.roots:
        p = Path(raw.strip()).expanduser()
        if not p.is_absolute():
            raise HTTPException(400, f"not an absolute path: {raw}")
        if not is_browsable(p, bases):
            raise HTTPException(400, f"{p} is outside the browsable locations ({', '.join(str(b) for b in bases)})")
        if p not in chosen:
            chosen.append(p)
    save_config(s.watcher_config_file, WatcherConfig(roots=[str(p) for p in chosen]))
    env_roots = _env_roots(request)
    w.set_roots(env_roots + [p for p in chosen if p not in env_roots])
    request.app.state.store.audit("admin", "watcher.roots", "watcher", None, {"roots": [str(r) for r in w.roots]})
    return roots(request)


@router.get("/browse")
def browse(request: Request, path: str | None = None) -> dict[str, Any]:
    """Folder picker: sub-folders (with video counts) and videos of `path`; without a path, the places to start from."""
    bases = _browsable_bases(request)
    if not path:
        return {"path": None, "parent": None, "folders": [{"name": str(b), "path": str(b), "videos": folder_status(b)["videos"]} for b in bases], "videos": [], "places": True}
    p = Path(path).expanduser()
    if not is_browsable(p, bases):
        raise HTTPException(400, f"{p} is outside the browsable locations")
    if not p.is_dir():
        raise HTTPException(404, f"folder not found: {p}")
    out = list_dir(p)
    if not is_browsable(Path(out.get("parent") or p), bases):
        out["parent"] = None   # stop at the top of the place
    out["places"] = False
    return out
