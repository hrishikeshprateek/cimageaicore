"""Watched folders chosen in the admin UI, persisted next to the watcher state (data/watcher_config.json).

Effective roots = folders from .env (WATCH_ROOTS / NAS_WATCH_DIR, read-only here) + folders picked in the UI.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from services.block_engine.sources import VIDEO_EXTENSIONS

_LOCK = threading.RLock()


class WatcherConfig(BaseModel):
    roots: list[str] = Field(default_factory=list)   # folders added through the UI (absolute paths)


def load_config(path: Path) -> WatcherConfig:
    if not path.exists():
        return WatcherConfig()
    try:
        return WatcherConfig.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a corrupt file just means "no UI roots"
        return WatcherConfig()


def save_config(path: Path, cfg: WatcherConfig) -> None:
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(cfg.model_dump_json(indent=1), encoding="utf-8")
        tmp.replace(path)


# ------------------------------------------------------------------ places the picker may browse
_PLACE_CANDIDATES = ("/nas", "/mnt", "/media", "/home", "/srv", "/Volumes", "/data")


def places(extra: list[Path]) -> list[Path]:
    """Top-level folders the picker can start from: mounted NAS locations + configured roots. Only what exists."""
    out: list[Path] = []
    for raw in list(_PLACE_CANDIDATES) + [str(p) for p in extra]:
        p = Path(raw)
        try:
            if p.is_dir() and p.resolve() not in [o.resolve() for o in out]:
                out.append(p)
        except OSError:
            continue
    return out


def is_browsable(path: Path, allowed: list[Path]) -> bool:
    """A folder inside one of the places (the picker never wanders into /etc or the code)."""
    try:
        r = path.resolve()
    except OSError:
        return False
    for base in allowed:
        try:
            b = base.resolve()
        except OSError:
            continue
        if r == b or b in r.parents:
            return True
    return False


def folder_status(path: Path, *, max_files: int = 2000) -> dict[str, Any]:
    """exists / readable / how many videos (capped) - what the UI shows next to each folder."""
    st: dict[str, Any] = {"path": str(path), "exists": path.is_dir(), "readable": False, "videos": 0, "folders": 0, "mounted": None}
    if not st["exists"]:
        return st
    try:
        n = f = 0
        for entry in path.iterdir():
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                f += 1
            elif entry.suffix.lower() in VIDEO_EXTENSIONS:
                n += 1
        st.update(readable=True, videos=n, folders=f)
        st["mounted"] = os.path.ismount(str(path)) if str(path) in ("/nas", "/mnt/nas", "/home/data") else None
    except OSError as exc:
        st["error"] = str(exc)
    return st


def list_dir(path: Path, *, max_entries: int = 500) -> dict[str, Any]:
    """Sub-folders (with their own video counts) and video files of one folder."""
    folders: list[dict[str, Any]] = []
    videos: list[dict[str, Any]] = []
    try:
        entries = sorted(path.iterdir(), key=lambda e: e.name.lower())
    except OSError as exc:
        return {"path": str(path), "error": str(exc), "folders": [], "videos": []}
    for entry in entries[:max_entries]:
        if entry.name.startswith("."):
            continue
        try:
            if entry.is_dir():
                vids = 0
                try:
                    vids = sum(1 for e in entry.iterdir() if e.is_file() and e.suffix.lower() in VIDEO_EXTENSIONS)
                except OSError:
                    pass
                folders.append({"name": entry.name, "path": str(entry), "videos": vids})
            elif entry.suffix.lower() in VIDEO_EXTENSIONS:
                stt = entry.stat()
                videos.append({"name": entry.name, "size": stt.st_size, "mtime": stt.st_mtime})
        except OSError:
            continue
    return {"path": str(path), "parent": str(path.parent) if path.parent != path else None, "folders": folders, "videos": videos}
