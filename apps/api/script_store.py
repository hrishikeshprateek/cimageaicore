"""PostgreSQL store for video scripts: one row per script, every edit keeps the previous scenes."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

ScriptStatus = Literal["generating", "new", "in_review", "approved", "rejected", "failed"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _id() -> str:
    return uuid.uuid4().hex[:12]


class Script(BaseModel):
    id: str
    status: ScriptStatus
    idea: str
    spoken: bool = False
    language: str = "hi"
    style: str = "viral_reel"
    target_seconds: int = 45
    job_id: str | None = None
    title: str | None = None
    hook: str | None = None
    cta: str | None = None
    caption: str | None = None
    scenes: list[dict[str, Any]] = Field(default_factory=list)
    hashtags: list[str] = Field(default_factory=list)
    extras: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)
    model: str | None = None
    prompt_version: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    version: int = 1
    created_at: datetime
    updated_at: datetime

    @property
    def planned_seconds(self) -> float:
        return round(sum(float(s.get("seconds") or 0) for s in self.scenes), 1)

    @property
    def words(self) -> int:
        return sum(len((s.get("voiceover") or "").split()) for s in self.scenes)

    def teleprompter(self) -> str:
        """Just the spoken words, scene by scene - what the presenter reads."""
        out = [self.hook or ""]
        for s in self.scenes:
            line = (s.get("voiceover") or "").strip()
            if line and line != (self.hook or "").strip():
                out.append(line)
        if self.cta:
            out.append(self.cta)
        return "\n\n".join(x for x in out if x)

    def as_markdown(self) -> str:
        """The shooting script, for download / sharing with whoever holds the camera."""
        head = [f"# {self.title or 'Script'}", "",
                f"*{self.target_seconds}s · {self.language} · {self.style.replace('_', ' ')} · {self.words} words · v{self.version}*", "",
                f"**Hook:** {self.hook or '-'}", ""]
        for s in self.scenes:
            extra = []
            if s.get("on_screen_text"):
                extra.append(f"On screen: {s['on_screen_text']}")
            if s.get("b_roll_block_id"):
                extra.append(f"Footage: {s['b_roll_block_id']}")
            if s.get("b_roll_image_id"):
                extra.append(f"Still: {s['b_roll_image_id']}")
            head += [f"## Scene {s.get('n')} · {s.get('seconds')}s", f"*{s.get('visual', '')}*", "", s.get("voiceover") or "",
                     ("> " + " · ".join(extra)) if extra else "", ""]
        head += [f"**CTA:** {self.cta or '-'}", "", f"**Caption:** {self.caption or '-'}", "",
                 " ".join(f"#{h.lstrip('#')}" for h in self.hashtags)]
        shots = self.extras.get("shot_list") or []
        if shots:
            head += ["", "## Still to film", *[f"- {x}" for x in shots]]
        return "\n".join(head).replace("\n\n\n", "\n\n")


_LIST_COLS = ("id, status, idea, spoken, language, style, target_seconds, job_id, title, hook, model, prompt_version, warnings, error, version, "
              "created_at, updated_at, scenes, '{}'::jsonb AS extras, '{}'::jsonb AS evidence, '{}'::jsonb AS usage, hashtags, cta, caption")


class ScriptStore:
    def __init__(self, pool: ConnectionPool):
        self.pool = pool

    def create(self, idea: str, *, language: str, style: str, target_seconds: int, job_id: str | None, spoken: bool = False) -> Script:
        sid = _id()
        with self.pool.connection() as conn, conn.transaction():
            conn.execute(
                "INSERT INTO scripts (id, idea, language, style, target_seconds, job_id, spoken) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (sid, idea, language, style, target_seconds, job_id, spoken),
            )
        return self.get(sid)

    def finish(self, sid: str, *, title: str, hook: str, cta: str, caption: str, scenes: list[dict], hashtags: list[str], extras: dict,
               evidence: dict, model: str, prompt_version: str, usage: dict, warnings: list[str], language: str | None = None) -> Script:
        with self.pool.connection() as conn, conn.transaction():
            conn.execute(
                """UPDATE scripts SET status = 'new', title = %s, hook = %s, cta = %s, caption = %s, scenes = %s, hashtags = %s, extras = %s,
                          evidence = %s, model = %s, prompt_version = %s, usage = %s, warnings = %s, language = COALESCE(%s, language),
                          error = NULL, updated_at = %s
                   WHERE id = %s""",
                (title, hook, cta, caption, Jsonb(scenes), Jsonb(hashtags), Jsonb(extras), Jsonb(evidence), model, prompt_version,
                 Jsonb(usage), Jsonb(warnings), language, _now(), sid),
            )
            conn.execute(
                "INSERT INTO script_versions (script_id, version, title, scenes, edited_by) SELECT id, version, title, scenes, 'agent' FROM scripts WHERE id = %s",
                (sid,),
            )
        return self.get(sid)

    def fail(self, sid: str, error: str) -> Script:
        with self.pool.connection() as conn, conn.transaction():
            conn.execute("UPDATE scripts SET status = 'failed', error = %s, updated_at = %s WHERE id = %s", (error, _now(), sid))
        return self.get(sid)

    def reset_for_regeneration(self, sid: str, *, language: str | None = None, style: str | None = None, target_seconds: int | None = None) -> Script:
        with self.pool.connection() as conn, conn.transaction():
            conn.execute(
                """UPDATE scripts SET status = 'generating', version = version + 1, error = NULL, language = COALESCE(%s, language),
                          style = COALESCE(%s, style), target_seconds = COALESCE(%s, target_seconds), updated_at = %s WHERE id = %s""",
                (language, style, target_seconds, _now(), sid),
            )
        return self.get(sid)

    def update(self, sid: str, *, title: str | None = None, hook: str | None = None, cta: str | None = None, caption: str | None = None,
               scenes: list[dict] | None = None, hashtags: list[str] | None = None, extras: dict[str, Any] | None = None,
               edited_by: str = "editor") -> Script:
        """Editor change: the previous scenes are kept, the version bumps and the script goes back to 'in review'."""
        with self.pool.connection() as conn, conn.transaction():
            conn.execute(
                "INSERT INTO script_versions (script_id, version, title, scenes, edited_by) SELECT id, version, title, scenes, 'previous' FROM scripts WHERE id = %s",
                (sid,),
            )
            conn.execute(
                """UPDATE scripts SET title = COALESCE(%s, title), hook = COALESCE(%s, hook), cta = COALESCE(%s, cta), caption = COALESCE(%s, caption),
                          scenes = COALESCE(%s, scenes), hashtags = COALESCE(%s, hashtags), extras = COALESCE(%s, extras), version = version + 1,
                          status = CASE WHEN status IN ('new', 'in_review') THEN 'in_review' ELSE status END, updated_at = %s
                   WHERE id = %s""",
                (title, hook, cta, caption, Jsonb(scenes) if scenes is not None else None, Jsonb(hashtags) if hashtags is not None else None,
                 Jsonb(extras) if extras is not None else None, _now(), sid),
            )
            conn.execute(
                "INSERT INTO script_versions (script_id, version, title, scenes, edited_by) SELECT id, version, title, scenes, %s FROM scripts WHERE id = %s",
                (edited_by, sid),
            )
        return self.get(sid)

    def set_status(self, sid: str, status: ScriptStatus) -> Script:
        with self.pool.connection() as conn, conn.transaction():
            conn.execute("UPDATE scripts SET status = %s, updated_at = %s WHERE id = %s", (status, _now(), sid))
        return self.get(sid)

    def delete(self, sid: str) -> bool:
        with self.pool.connection() as conn, conn.transaction():
            return conn.execute("DELETE FROM scripts WHERE id = %s", (sid,)).rowcount > 0

    def get(self, sid: str) -> Script | None:
        with self.pool.connection() as conn:
            r = conn.execute("SELECT * FROM scripts WHERE id = %s", (sid,)).fetchone()
            return Script.model_validate(r) if r else None

    def list(self, status: str | None = None, limit: int = 200) -> list[Script]:
        with self.pool.connection() as conn:
            sql = f"SELECT {_LIST_COLS} FROM scripts"
            params: list[Any] = []
            if status:
                sql += " WHERE status = %s"; params.append(status)
            rows = conn.execute(sql + " ORDER BY created_at DESC LIMIT %s", [*params, limit]).fetchall()
            return [Script.model_validate(r) for r in rows]

    def versions(self, sid: str) -> list[dict[str, Any]]:
        with self.pool.connection() as conn:
            return conn.execute(
                "SELECT version, title, edited_by, created_at, jsonb_array_length(scenes) AS scenes FROM script_versions WHERE script_id = %s ORDER BY id",
                (sid,),
            ).fetchall()
