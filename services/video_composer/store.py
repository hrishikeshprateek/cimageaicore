"""`renders` records: PostgreSQL (shares the app's pool) or JSON files under data/renders/records/, plus a background worker."""
from __future__ import annotations

import json
import logging
import queue
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

from services.video_composer.renderer import RenderSpec

log = logging.getLogger(__name__)

RenderStatus = Literal["QUEUED", "RENDERING", "DONE", "FAILED"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class RenderRecord(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    job_id: str
    media_id: str | None = None
    status: RenderStatus = "QUEUED"
    preset: str
    template: str
    cut_in: float
    cut_out: float
    title: str | None = None
    cut_id: str | None = None
    spec: RenderSpec
    output_path: str | None = None
    captions_path: str | None = None
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    size_bytes: int | None = None
    ffmpeg_command: str | None = None
    render_seconds: float | None = None
    error: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


_UPDATABLE = {"status", "output_path", "captions_path", "width", "height", "duration_seconds", "size_bytes", "ffmpeg_command", "render_seconds", "error", "detail"}


class JsonRenderStore:
    kind = "json"

    def __init__(self, records_dir: Path):
        self.dir = records_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, render_id: str) -> Path:
        return self.dir / f"{render_id}.json"

    def _write(self, rec: RenderRecord) -> None:
        self._path(rec.id).write_text(rec.model_dump_json(indent=2), encoding="utf-8")

    def create(self, rec: RenderRecord) -> RenderRecord:
        with self._lock:
            self._write(rec)
        return rec

    def get(self, render_id: str) -> RenderRecord | None:
        p = self._path(render_id)
        if not p.exists():
            return None
        with self._lock:
            return RenderRecord.model_validate_json(p.read_text(encoding="utf-8"))

    def list(self, job_id: str | None = None, limit: int = 200) -> list[RenderRecord]:
        out = []
        with self._lock:
            for p in self.dir.glob("*.json"):
                try:
                    rec = RenderRecord.model_validate_json(p.read_text(encoding="utf-8"))
                except Exception as exc:  # noqa: BLE001
                    log.warning("skipping unreadable render record %s: %s", p.name, exc)
                    continue
                if job_id is None or rec.job_id == job_id:
                    out.append(rec)
        out.sort(key=lambda r: r.created_at, reverse=True)
        return out[:limit]

    def update(self, render_id: str, **fields: Any) -> RenderRecord:
        bad = set(fields) - _UPDATABLE
        if bad:
            raise ValueError(f"cannot update {bad}")
        with self._lock:
            rec = self.get(render_id)
            if rec is None:
                raise KeyError(render_id)
            rec = rec.model_copy(update={**fields, "updated_at": _now()})
            self._write(rec)
            return rec

    def delete(self, render_id: str) -> bool:
        with self._lock:
            p = self._path(render_id)
            existed = p.exists()
            p.unlink(missing_ok=True)
            return existed

    def mark_stale(self) -> int:
        """Renders left QUEUED/RENDERING by a previous process cannot resume -> FAILED."""
        n = 0
        for rec in self.list():
            if rec.status in ("QUEUED", "RENDERING"):
                self.update(rec.id, status="FAILED", error="interrupted by restart")
                n += 1
        return n


class PostgresRenderStore:
    kind = "postgres"

    _COLS = "id, job_id, media_id, status, preset, template, cut_in, cut_out, title, cut_id, spec, output_path, captions_path, width, height, duration_seconds, size_bytes, ffmpeg_command, render_seconds, error, detail, created_at, updated_at"

    def __init__(self, pool):
        self.pool = pool

    @staticmethod
    def _row(r: dict[str, Any]) -> RenderRecord:
        return RenderRecord(**{**r, "spec": RenderSpec.model_validate(r["spec"]), "detail": r["detail"] or {}})

    def create(self, rec: RenderRecord) -> RenderRecord:
        from psycopg.types.json import Jsonb

        with self.pool.connection() as conn, conn.transaction():
            conn.execute(
                f"""INSERT INTO renders ({self._COLS})
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (rec.id, rec.job_id, rec.media_id, rec.status, rec.preset, rec.template, rec.cut_in, rec.cut_out, rec.title, rec.cut_id,
                 Jsonb(rec.spec.model_dump(mode="json")), rec.output_path, rec.captions_path, rec.width, rec.height, rec.duration_seconds, rec.size_bytes,
                 rec.ffmpeg_command, rec.render_seconds, rec.error, Jsonb(rec.detail), rec.created_at, rec.updated_at),
            )
            conn.execute("INSERT INTO audit_log (actor, action, entity_type, entity_id, detail) VALUES (%s, %s, %s, %s, %s)",
                         ("composer", "render.created", "render", rec.id, Jsonb({"job_id": rec.job_id, "preset": rec.preset, "cut": [rec.cut_in, rec.cut_out]})))
        return rec

    def get(self, render_id: str) -> RenderRecord | None:
        with self.pool.connection() as conn:
            r = conn.execute(f"SELECT {self._COLS} FROM renders WHERE id = %s", (render_id,)).fetchone()
            return self._row(r) if r else None

    def list(self, job_id: str | None = None, limit: int = 200) -> list[RenderRecord]:
        with self.pool.connection() as conn:
            if job_id:
                rows = conn.execute(f"SELECT {self._COLS} FROM renders WHERE job_id = %s ORDER BY created_at DESC LIMIT %s", (job_id, limit)).fetchall()
            else:
                rows = conn.execute(f"SELECT {self._COLS} FROM renders ORDER BY created_at DESC LIMIT %s", (limit,)).fetchall()
            return [self._row(r) for r in rows]

    def update(self, render_id: str, **fields: Any) -> RenderRecord:
        from psycopg.types.json import Jsonb

        bad = set(fields) - _UPDATABLE
        if bad:
            raise ValueError(f"cannot update {bad}")
        sets = ", ".join(f"{k} = %s" for k in fields) + ", updated_at = %s"
        vals = [Jsonb(v) if k == "detail" else v for k, v in fields.items()] + [_now(), render_id]
        with self.pool.connection() as conn, conn.transaction():
            conn.execute(f"UPDATE renders SET {sets} WHERE id = %s", vals)
            if "status" in fields:
                conn.execute("INSERT INTO audit_log (actor, action, entity_type, entity_id, detail) VALUES (%s, %s, %s, %s, %s)",
                             ("composer", f"render.{fields['status'].lower()}", "render", render_id, Jsonb({k: v for k, v in fields.items() if k in ("error", "output_path", "render_seconds")})))
            r = conn.execute(f"SELECT {self._COLS} FROM renders WHERE id = %s", (render_id,)).fetchone()
            if r is None:
                raise KeyError(render_id)
            return self._row(r)

    def delete(self, render_id: str) -> bool:
        with self.pool.connection() as conn, conn.transaction():
            cur = conn.execute("DELETE FROM renders WHERE id = %s", (render_id,))
            return (cur.rowcount or 0) > 0

    def mark_stale(self) -> int:
        with self.pool.connection() as conn, conn.transaction():
            cur = conn.execute("UPDATE renders SET status = 'FAILED', error = 'interrupted by restart', updated_at = now() WHERE status IN ('QUEUED', 'RENDERING')")
            return cur.rowcount or 0


class RenderWorker:
    """Daemon threads draining a queue of render ids; a killed server abandons the render and
    `mark_stale()` flags it at the next start (same policy as processing_jobs)."""

    def __init__(self, store, run: Callable[[RenderRecord], None], threads: int = 1):
        self.store = store
        self.run = run
        self.q: queue.Queue[str] = queue.Queue()
        self.threads = [threading.Thread(target=self._loop, name=f"render-worker-{i}", daemon=True) for i in range(max(1, threads))]
        for t in self.threads:
            t.start()

    def submit(self, render_id: str) -> None:
        self.q.put(render_id)

    def _loop(self) -> None:
        while True:
            render_id = self.q.get()
            try:
                rec = self.store.get(render_id)
                if rec is None or rec.status != "QUEUED":
                    continue
                self.store.update(render_id, status="RENDERING")
                self.run(rec)
            except Exception as exc:  # noqa: BLE001 - every failure must land in the record
                log.exception("render %s failed", render_id)
                try:
                    self.store.update(render_id, status="FAILED", error=f"{type(exc).__name__}: {exc}"[:2000])
                except Exception:  # noqa: BLE001
                    log.exception("could not record failure for render %s", render_id)
            finally:
                self.q.task_done()
