"""publish_targets + publications tables. Secrets never leave this module unencrypted except into a client."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from services.publishing.crypto import SecretBox

Mode = Literal["draft", "publish"]
PubStatus = Literal["queued", "publishing", "draft", "published", "failed"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TargetLayout(BaseModel):
    """How the article is laid out on this site (defaults = cimage.in house style)."""

    hero_in_body: bool = Field(default=True, description="Put the hero image in the body after the intro paragraph (templates often hide the featured image).")
    image_position: Literal["under_heading", "as_placed"] = Field(default="under_heading", description="Each section's picture directly under its heading, or where the writer placed it.")
    image_captions: bool = Field(default=False, description="Show captions under pictures.")


class Target(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    kind: Literal["wordpress"] = "wordpress"
    name: str
    url: str
    username: str
    mode: Mode = "draft"
    auto_on_approval: bool = True
    enabled: bool = True
    default_category: str | None = None
    seo_plugin: Literal["none", "yoast", "rankmath"] = "none"
    rest_prefix: str = "/wp-json"
    layout: TargetLayout = Field(default_factory=TargetLayout)
    site_title: str | None = None
    last_test_at: datetime | None = None
    last_test_ok: bool | None = None
    last_error: str | None = None
    has_secret: bool = True
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class Publication(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    draft_id: str
    target_id: str
    status: PubStatus = "queued"
    mode: Mode
    remote_id: str | None = None
    remote_url: str | None = None
    edit_url: str | None = None
    draft_version: int | None = None
    media_map: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    triggered_by: str = "editor"
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    target_name: str | None = None
    target_url: str | None = None


_T_COLS = "id, kind, name, url, username, mode, auto_on_approval, enabled, default_category, seo_plugin, rest_prefix, layout, site_title, last_test_at, last_test_ok, last_error, created_at, updated_at"
_P_COLS = "p.id, p.draft_id, p.target_id, p.status, p.mode, p.remote_id, p.remote_url, p.edit_url, p.draft_version, p.media_map, p.error, p.detail, p.triggered_by, p.created_at, p.updated_at, t.name AS target_name, t.url AS target_url"
_T_UPDATABLE = {"name", "url", "username", "mode", "auto_on_approval", "enabled", "default_category", "seo_plugin", "rest_prefix", "layout", "site_title", "last_test_at", "last_test_ok", "last_error"}
_P_UPDATABLE = {"status", "mode", "remote_id", "remote_url", "edit_url", "draft_version", "media_map", "error", "detail", "triggered_by"}


class PublishStore:
    def __init__(self, pool, box: SecretBox):
        self.pool = pool
        self.box = box

    # ------------------------------------------------------------------ targets
    def create_target(self, t: Target, secret: str) -> Target:
        with self.pool.connection() as conn, conn.transaction():
            conn.execute(
                f"INSERT INTO publish_targets ({_T_COLS}, secret_enc) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (t.id, t.kind, t.name, t.url.rstrip("/"), t.username, t.mode, t.auto_on_approval, t.enabled, t.default_category, t.seo_plugin, t.rest_prefix,
                 Jsonb(t.layout.model_dump()), t.site_title, t.last_test_at, t.last_test_ok, t.last_error, t.created_at, t.updated_at, self.box.encrypt(secret)),
            )
        return self.get_target(t.id)

    def get_target(self, target_id: str) -> Target | None:
        with self.pool.connection() as conn:
            r = conn.execute(f"SELECT {_T_COLS} FROM publish_targets WHERE id = %s", (target_id,)).fetchone()
            return Target.model_validate(r) if r else None

    def list_targets(self, *, enabled_only: bool = False) -> list[Target]:
        with self.pool.connection() as conn:
            sql = f"SELECT {_T_COLS} FROM publish_targets" + (" WHERE enabled" if enabled_only else "") + " ORDER BY created_at"
            return [Target.model_validate(r) for r in conn.execute(sql).fetchall()]

    def update_target(self, target_id: str, *, secret: str | None = None, **fields: Any) -> Target | None:
        bad = set(fields) - _T_UPDATABLE
        if bad:
            raise ValueError(f"cannot update {bad}")
        if "url" in fields and fields["url"]:
            fields["url"] = fields["url"].rstrip("/")
        if "layout" in fields:
            lay = fields["layout"]
            fields["layout"] = Jsonb(lay.model_dump() if isinstance(lay, TargetLayout) else TargetLayout.model_validate(lay or {}).model_dump())
        sets = [f"{k} = %s" for k in fields]
        vals: list[Any] = list(fields.values())
        if secret:
            sets.append("secret_enc = %s")
            vals.append(self.box.encrypt(secret))
        sets.append("updated_at = %s")
        vals += [_now(), target_id]
        with self.pool.connection() as conn, conn.transaction():
            conn.execute(f"UPDATE publish_targets SET {', '.join(sets)} WHERE id = %s", vals)
        return self.get_target(target_id)

    def delete_target(self, target_id: str) -> bool:
        with self.pool.connection() as conn, conn.transaction():
            return (conn.execute("DELETE FROM publish_targets WHERE id = %s", (target_id,)).rowcount or 0) > 0

    def secret_for(self, target_id: str) -> str:
        with self.pool.connection() as conn:
            r = conn.execute("SELECT secret_enc FROM publish_targets WHERE id = %s", (target_id,)).fetchone()
        if r is None:
            raise KeyError(target_id)
        return self.box.decrypt(r["secret_enc"])

    # ------------------------------------------------------------------ publications
    def upsert_publication(self, draft_id: str, target_id: str, *, mode: str, triggered_by: str) -> Publication:
        """One row per (draft, target); a re-publish reuses it (and its media_map)."""
        with self.pool.connection() as conn, conn.transaction():
            r = conn.execute("SELECT id FROM publications WHERE draft_id = %s AND target_id = %s", (draft_id, target_id)).fetchone()
            if r:
                conn.execute("UPDATE publications SET status = 'queued', mode = %s, error = NULL, triggered_by = %s, updated_at = %s WHERE id = %s",
                             (mode, triggered_by, _now(), r["id"]))
                pid = r["id"]
            else:
                p = Publication(draft_id=draft_id, target_id=target_id, mode=mode, triggered_by=triggered_by)
                conn.execute(
                    "INSERT INTO publications (id, draft_id, target_id, status, mode, media_map, detail, triggered_by, created_at, updated_at) VALUES (%s, %s, %s, 'queued', %s, '{}', '{}', %s, %s, %s)",
                    (p.id, draft_id, target_id, mode, triggered_by, p.created_at, p.updated_at),
                )
                pid = p.id
        return self.get_publication(pid)

    def get_publication(self, pub_id: str) -> Publication | None:
        with self.pool.connection() as conn:
            r = conn.execute(f"SELECT {_P_COLS} FROM publications p JOIN publish_targets t ON t.id = p.target_id WHERE p.id = %s", (pub_id,)).fetchone()
            return Publication.model_validate(r) if r else None

    def list_publications(self, *, draft_id: str | None = None, limit: int = 200) -> list[Publication]:
        with self.pool.connection() as conn:
            sql = f"SELECT {_P_COLS} FROM publications p JOIN publish_targets t ON t.id = p.target_id"
            params: list[Any] = []
            if draft_id:
                sql += " WHERE p.draft_id = %s"; params.append(draft_id)
            rows = conn.execute(sql + " ORDER BY p.updated_at DESC LIMIT %s", [*params, limit]).fetchall()
            return [Publication.model_validate(r) for r in rows]

    def update_publication(self, pub_id: str, **fields: Any) -> Publication:
        bad = set(fields) - _P_UPDATABLE
        if bad:
            raise ValueError(f"cannot update {bad}")
        sets = ", ".join(f"{k} = %s" for k in fields) + ", updated_at = %s"
        vals = [Jsonb(v) if k in ("media_map", "detail") else v for k, v in fields.items()] + [_now(), pub_id]
        with self.pool.connection() as conn, conn.transaction():
            conn.execute(f"UPDATE publications SET {sets} WHERE id = %s", vals)
        p = self.get_publication(pub_id)
        if p is None:
            raise KeyError(pub_id)
        return p
