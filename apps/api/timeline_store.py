"""Saved edits: Postgres when it is there, JSON files otherwise. One row per timeline."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.video_composer.timeline import Timeline

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _row(t: Timeline) -> dict[str, Any]:
    return {"id": t.id, "title": t.title, "job_id": (t.jobs[0] if t.jobs else None), "preset": t.preset,
            "template": t.template, "fit": t.fit, "audio": t.audio, "seconds": t.seconds,
            "clips": [c.model_dump(mode="json") for c in t.clips], "source": t.source}


class JsonTimelineStore:
    kind = "json"

    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)

    def _p(self, tid: str) -> Path:
        return self.dir / f"{tid}.json"

    def put(self, t: Timeline) -> Timeline:
        self._p(t.id).write_text(t.model_dump_json(indent=1), encoding="utf-8")
        return t

    def get(self, tid: str) -> Timeline | None:
        p = self._p(tid)
        if not p.exists():
            return None
        try:
            return Timeline.model_validate_json(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None

    def list(self, job_id: str | None = None, limit: int = 100) -> list[Timeline]:
        out = []
        for p in sorted(self.dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[: limit * 2]:
            t = self.get(p.stem)
            if t and (job_id is None or job_id in t.jobs):
                out.append(t)
        return out[:limit]

    def delete(self, tid: str) -> bool:
        p = self._p(tid)
        if p.exists():
            p.unlink()
            return True
        return False


class PostgresTimelineStore:
    kind = "postgres"
    _COLS = "id, title, job_id, preset, template, fit, audio, seconds, clips, source, created_at, updated_at"

    def __init__(self, pool):
        self.pool = pool

    @staticmethod
    def _load(r: dict[str, Any]) -> Timeline:
        return Timeline.model_validate({**{k: r[k] for k in ("id", "title", "preset", "template", "fit", "audio", "source")},
                                        "clips": r["clips"] or [], "created_at": r["created_at"]})

    def put(self, t: Timeline) -> Timeline:
        from psycopg.types.json import Jsonb

        d = _row(t)
        with self.pool.connection() as conn, conn.transaction():
            conn.execute(
                """INSERT INTO timelines (id, title, job_id, preset, template, fit, audio, seconds, clips, source, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                   ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title, job_id = EXCLUDED.job_id, preset = EXCLUDED.preset,
                        template = EXCLUDED.template, fit = EXCLUDED.fit, audio = EXCLUDED.audio, seconds = EXCLUDED.seconds,
                        clips = EXCLUDED.clips, source = EXCLUDED.source, updated_at = now()""",
                (d["id"], d["title"], d["job_id"], d["preset"], d["template"], d["fit"], d["audio"], d["seconds"],
                 Jsonb(d["clips"]), Jsonb(d["source"])),
            )
        return t

    def get(self, tid: str) -> Timeline | None:
        with self.pool.connection() as conn:
            r = conn.execute(f"SELECT {self._COLS} FROM timelines WHERE id = %s", (tid,)).fetchone()
        return self._load(r) if r else None

    def list(self, job_id: str | None = None, limit: int = 100) -> list[Timeline]:
        sql = f"SELECT {self._COLS} FROM timelines"
        params: list[Any] = []
        if job_id:
            sql += " WHERE job_id = %s"
            params.append(job_id)
        with self.pool.connection() as conn:
            rows = conn.execute(sql + " ORDER BY updated_at DESC LIMIT %s", [*params, limit]).fetchall()
        return [self._load(r) for r in rows]

    def delete(self, tid: str) -> bool:
        with self.pool.connection() as conn, conn.transaction():
            return conn.execute("DELETE FROM timelines WHERE id = %s", (tid,)).rowcount > 0


def build_timeline_store(store, directory: Path):
    if getattr(store, "supports_vectors", False):
        return PostgresTimelineStore(store.pool)
    return JsonTimelineStore(directory)
