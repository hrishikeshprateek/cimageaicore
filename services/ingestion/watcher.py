"""Folder watcher (V0.4): the NAS drops videos, the platform picks them up on its own.

Polling, not inotify: SMB/NFS shares and Docker bind mounts don't deliver file events reliably, and a scan of a
few thousand paths every 30 s costs nothing. Each cycle:

  1. walk the watched roots for video files (hidden / temp / partial files skipped)
  2. a file is *stable* when its (size, mtime) is unchanged across two scans at least `stable_seconds` apart
     - copies in progress are never picked up half-way
  3. stable files not yet in the index are submitted through apps.api.ingest.submit_source, which hashes the
     bytes and dedupes against the store (the same video under two names = one job); at most `max_active_jobs`
     analyses run at once so a folder full of old footage doesn't burn the Gemini quota in one go
  4. the embedding sweeper embeds blocks that still lack a vector (or carry a vector from a previous model)
     - a local-model outage heals itself on the next cycle
  5. the index (path -> size/mtime/job) is persisted, so a restart doesn't rehash the whole NAS

Runs as a daemon thread inside the API process (no Redis needed yet); everything it does is visible in
GET /api/v1/watcher and the audit log.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from services.block_engine.sources import VIDEO_EXTENSIONS, SourceError

log = logging.getLogger(__name__)

SKIP_PREFIXES = (".", "~$", "._")
SKIP_SUFFIXES = (".part", ".tmp", ".crdownload", ".partial", ".download")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_candidate(path: Path) -> bool:
    """A video file we would consider: right extension, not hidden/temp/partial, not inside a hidden directory."""
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        return False
    if path.name.startswith(SKIP_PREFIXES) or path.name.lower().endswith(SKIP_SUFFIXES):
        return False
    return not any(part.startswith(".") for part in path.parts[:-1] if part not in (".", ".."))


def scan(roots: list[Path]) -> dict[str, tuple[int, int]]:
    """path -> (size, mtime_ns) for every candidate video under the roots. Unreadable entries are skipped, never fatal."""
    found: dict[str, tuple[int, int]] = {}
    for root in roots:
        if not root.exists():
            continue
        try:
            for p in root.rglob("*"):
                try:
                    if p.is_file() and is_candidate(p):
                        st = p.stat()
                        found[str(p)] = (st.st_size, st.st_mtime_ns)
                except OSError:
                    continue
        except OSError as exc:
            log.warning("cannot scan %s: %s", root, exc)
    return found


@dataclass
class Event:
    at: str
    action: str            # queued | deduplicated | skipped | error | embedded
    path: str | None = None
    job_id: str | None = None
    detail: str | None = None


@dataclass
class WatcherStatus:
    enabled: bool
    running: bool
    paused: bool
    roots: list[str]
    interval_seconds: float
    stable_seconds: float
    max_active_jobs: int
    last_scan_at: str | None = None
    next_scan_at: str | None = None
    last_scan_seconds: float | None = None
    files_seen: int = 0
    pending_stable: int = 0            # seen but not yet stable / waiting for a free worker slot
    indexed: int = 0                   # files already handed to the pipeline (this index)
    queued_total: int = 0
    deduplicated_total: int = 0
    errors_total: int = 0
    embedded_total: int = 0
    active_jobs: int = 0
    scans: int = 0
    recent: list[dict[str, Any]] = field(default_factory=list)


class FolderWatcher:
    """See module docstring. `submit(path) -> (job_id, deduplicated)` and `sweep_embeddings() -> int` are injected so the
    watcher knows nothing about FastAPI, Gemini or Postgres and is testable with a temp folder."""

    def __init__(self, roots: list[Path], *, submit: Callable[[Path], tuple[str, bool]], active_jobs: Callable[[], int],
                 sweep_embeddings: Callable[[], int] | None = None, interval_seconds: float = 30.0, stable_seconds: float = 5.0,
                 max_active_jobs: int = 2, state_file: Path | None = None, enabled: bool = True):
        self.roots = [Path(r) for r in roots]
        self.submit = submit
        self.active_jobs = active_jobs
        self.sweep_embeddings = sweep_embeddings
        self.interval = max(1.0, float(interval_seconds))
        self.stable_seconds = max(0.0, float(stable_seconds))
        self.max_active_jobs = max(1, int(max_active_jobs))
        self.state_file = state_file
        self.enabled = enabled
        self.paused = False
        self._index: dict[str, dict[str, Any]] = {}     # path -> {size, mtime, job_id, action, at}
        self._seen: dict[str, tuple[int, int, float]] = {}   # path -> (size, mtime, first-seen-unchanged monotonic)
        self._recent: deque[Event] = deque(maxlen=100)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._status = WatcherStatus(enabled=enabled, running=False, paused=False, roots=[str(r) for r in self.roots],
                                     interval_seconds=self.interval, stable_seconds=self.stable_seconds, max_active_jobs=self.max_active_jobs)
        self._load()

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="folder-watcher", daemon=True)
        self._thread.start()
        self._status.running = True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
        self._status.running = False
        self._save()

    def trigger(self) -> None:
        """Scan now instead of waiting for the interval."""
        self._wake.set()

    def set_roots(self, roots: list[Path]) -> None:
        """Change the watched folders while running (the admin folder picker). Files under a removed root stay in the index."""
        with self._lock:
            self.roots = [Path(r) for r in roots]
            self._status.roots = [str(r) for r in self.roots]
            self._seen = {k: v for k, v in self._seen.items() if any(k.startswith(str(r)) for r in self.roots)}
        self._event("roots_changed", detail=", ".join(str(r) for r in self.roots)[:300])
        self._wake.set()

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        self._status.paused = paused
        self._event("paused" if paused else "resumed")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception as exc:  # noqa: BLE001 - the loop must survive anything
                log.exception("watcher cycle failed")
                self._event("error", detail=f"{type(exc).__name__}: {exc}"[:300])
                self._status.errors_total += 1
            self._status.next_scan_at = datetime.fromtimestamp(time.time() + self.interval, timezone.utc).isoformat(timespec="seconds")
            self._wake.wait(self.interval)
            self._wake.clear()

    # ------------------------------------------------------------------ one cycle
    def scan_once(self) -> dict[str, int]:
        """One full cycle. Returns counts; also used directly by tests and the /watcher/scan endpoint."""
        t0 = time.monotonic()
        counts = {"seen": 0, "queued": 0, "deduplicated": 0, "skipped": 0, "errors": 0, "embedded": 0}
        found = scan(self.roots)
        counts["seen"] = len(found)
        now = time.monotonic()
        with self._lock:
            for path in list(self._seen):
                if path not in found:
                    del self._seen[path]
            pending = 0
            for path, (size, mtime) in sorted(found.items()):
                idx = self._index.get(path)
                if idx and idx.get("size") == size and idx.get("mtime") == mtime:
                    continue   # already handled, unchanged
                prev = self._seen.get(path)
                if prev is None or prev[0] != size or prev[1] != mtime:
                    self._seen[path] = (size, mtime, now)   # (re)started the stability clock
                    pending += 1
                    continue
                if now - prev[2] < self.stable_seconds:
                    pending += 1
                    continue
                if self.paused:
                    pending += 1
                    continue
                if self.active_jobs() >= self.max_active_jobs:
                    pending += 1
                    continue   # leave it for the next cycle; keeps the Gemini quota under control
                self._handle(Path(path), size, mtime, counts)
            self._status.pending_stable = pending
        if self.sweep_embeddings is not None and not self.paused:
            try:
                n = self.sweep_embeddings()
                if n:
                    counts["embedded"] = n
                    self._status.embedded_total += n
                    self._event("embedded", detail=f"{n} blocks")
            except Exception as exc:  # noqa: BLE001 - embedder down: try again next cycle
                log.warning("embedding sweep failed: %s", exc)
                self._event("error", detail=f"embedding sweep: {exc}"[:300])
                counts["errors"] += 1
        st = self._status
        st.scans += 1
        st.files_seen = counts["seen"]
        st.indexed = len(self._index)
        st.last_scan_at = _now_iso()
        st.last_scan_seconds = round(time.monotonic() - t0, 3)
        st.active_jobs = self.active_jobs()
        self._save()
        return counts

    def _handle(self, path: Path, size: int, mtime: int, counts: dict[str, int]) -> None:
        try:
            job_id, dedup = self.submit(path)
        except SourceError as exc:      # e.g. not a decodable video: remember it so we don't retry every cycle
            self._index[str(path)] = {"size": size, "mtime": mtime, "job_id": None, "action": "skipped", "at": _now_iso(), "detail": str(exc)[:300]}
            self._event("skipped", path=str(path), detail=str(exc)[:300])
            counts["skipped"] += 1
            return
        except Exception as exc:  # noqa: BLE001 - transient (store down, hashing I/O error): retry next cycle
            self._event("error", path=str(path), detail=f"{type(exc).__name__}: {exc}"[:300])
            self._status.errors_total += 1
            counts["errors"] += 1
            self._seen.pop(str(path), None)
            return
        action = "deduplicated" if dedup else "queued"
        self._index[str(path)] = {"size": size, "mtime": mtime, "job_id": job_id, "action": action, "at": _now_iso()}
        self._seen.pop(str(path), None)
        self._event(action, path=str(path), job_id=job_id)
        counts[action] += 1
        if dedup:
            self._status.deduplicated_total += 1
        else:
            self._status.queued_total += 1

    # ------------------------------------------------------------------ status / persistence
    def _event(self, action: str, *, path: str | None = None, job_id: str | None = None, detail: str | None = None) -> None:
        self._recent.appendleft(Event(at=_now_iso(), action=action, path=path, job_id=job_id, detail=detail))

    def status(self) -> WatcherStatus:
        st = self._status
        st.recent = [e.__dict__ for e in list(self._recent)[:50]]
        st.indexed = len(self._index)
        st.paused = self.paused
        return st

    def index(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return dict(self._index)

    def forget(self, path: str) -> bool:
        """Drop a path from the index so the next scan re-submits it (after the file was replaced, or to re-analyse)."""
        with self._lock:
            return self._index.pop(path, None) is not None

    def _load(self) -> None:
        if self.state_file and self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                self._index = dict(data.get("index", {}))
                for k in ("queued_total", "deduplicated_total", "errors_total", "embedded_total"):
                    setattr(self._status, k, int(data.get(k, 0)))
            except Exception as exc:  # noqa: BLE001 - a corrupt state file just means one full rescan
                log.warning("watcher state unreadable (%s); starting with an empty index", exc)

    def _save(self) -> None:
        if not self.state_file:
            return
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            st = self._status
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"index": self._index, "queued_total": st.queued_total, "deduplicated_total": st.deduplicated_total,
                                       "errors_total": st.errors_total, "embedded_total": st.embedded_total, "saved_at": _now_iso()}, indent=1),
                           encoding="utf-8")
            tmp.replace(self.state_file)
        except OSError as exc:
            log.warning("could not save watcher state: %s", exc)
