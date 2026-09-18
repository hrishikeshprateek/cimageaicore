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
ANYWHERE = Path("/")
# Inside the api container the filesystem is the image's, not the server's: only what docker-compose mounts is visible
# (/nas = NAS_WATCH_DIR, the host's /mnt and /media under the same paths, /data = the app's own files).
IN_DOCKER = Path("/.dockerenv").exists()
_DOCKER_ALWAYS = ("/nas", "/mnt", "/media")   # listed even when empty - that is where the host's shares are expected to show up
_DOCKER_HIDDEN = ("/data",)                   # the app's own data volume (proxies, renders): never a place to watch
_DOCKER_ROOT_NOISE = {"app", "root", "tmp", "data", "home", "srv"}   # the image's own folders when listing the container's /
# OS internals hidden when browsing from / - nobody keeps footage there and listing them is slow
_ROOT_NOISE = {"bin", "sbin", "usr", "etc", "var", "dev", "proc", "sys", "run", "boot", "lib", "lib32", "lib64", "libx32", "snap", "lost+found",
               "cores", "private", "System", "Library", "Applications", "opt"}


def _is_empty(p: Path) -> bool:
    try:
        return next(p.iterdir(), None) is None
    except OSError:
        return True


def places(extra: list[Path], *, anywhere: bool = True, in_docker: bool | None = None) -> list[Path]:
    """Starting points for the picker: home + Desktop/Downloads, mounted drives / NAS mounts, configured roots and -
    when `anywhere` (NAS_BROWSE_ANYWHERE, the default) - the whole filesystem. Only folders that exist.
    In Docker the container's /root is nobody's home and the image's empty /home, /srv... are noise, so the places are the
    mounted host folders (/nas, /mnt, /media - shown even while empty, so it is obvious where a share has to be mounted)."""
    in_docker = IN_DOCKER if in_docker is None else in_docker
    home = Path.home()
    starts = [home, home / "Desktop", home / "Downloads", home / "Movies", home / "Videos"] if anywhere and not in_docker else []
    candidates = [str(p) for p in starts] + list(_PLACE_CANDIDATES) + [str(p) for p in extra] + (["/"] if anywhere else [])
    out: list[Path] = []
    for raw in candidates:
        p = Path(raw)
        try:
            if not p.is_dir():
                continue
            if in_docker and raw in _PLACE_CANDIDATES and (raw in _DOCKER_HIDDEN or (raw not in _DOCKER_ALWAYS and _is_empty(p))):
                continue
            if p.resolve() not in [o.resolve() for o in out]:
                out.append(p)
        except OSError:
            continue
    return out


def place_label(p: Path, *, in_docker: bool | None = None) -> str:
    """Human name for a starting point ("Home", "Desktop", "Whole system", or the path itself)."""
    in_docker = IN_DOCKER if in_docker is None else in_docker
    home = Path.home()
    if p == ANYWHERE:
        return "Container filesystem  /" if in_docker else "Whole system  /"
    if p == home:
        return f"Home  {home}"
    if p.parent == home and p.name in ("Desktop", "Downloads", "Movies", "Videos"):
        return f"{p.name}  {p}"
    if str(p) == "/Volumes":
        return "Mounted drives  /Volumes"
    if str(p) in ("/mnt", "/media"):
        return f"{'Host mounts' if in_docker else 'Mounts'}  {p}"
    if str(p) == "/nas":
        return "NAS (NAS_WATCH_DIR)  /nas" if in_docker else "NAS  /nas"
    return str(p)


def is_browsable(path: Path, allowed: list[Path]) -> bool:
    """A folder inside one of the places. With the whole filesystem as a place (NAS_BROWSE_ANYWHERE) everything qualifies;
    the listing still hides dot-folders and system internals, and the watcher only ever *reads* what it is pointed at."""
    try:
        r = path.resolve()
    except OSError:
        return False
    if any(base == ANYWHERE for base in allowed):
        return True
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
        # is a share actually mounted here (a NAS folder that exists but is not mounted is just an empty directory)?
        st["mounted"] = os.path.ismount(str(path)) if str(path) == "/nas" or str(path.parent) in ("/mnt", "/media", "/Volumes") else None
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
    at_root = path.resolve() == ANYWHERE
    for entry in entries[:max_entries]:
        if entry.name.startswith(".") or (at_root and (entry.name in _ROOT_NOISE or (IN_DOCKER and entry.name in _DOCKER_ROOT_NOISE))):
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
