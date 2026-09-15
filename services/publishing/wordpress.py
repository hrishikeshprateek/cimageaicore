"""Minimal WordPress REST client (application passwords over HTTPS): connection test, media, tags/categories, posts."""
from __future__ import annotations

import base64
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)
TRANSPORT: httpx.BaseTransport | None = None      # tests inject an httpx.MockTransport here


class WordPressError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class WordPressClient:
    def __init__(self, url: str, username: str, app_password: str, *, rest_prefix: str = "/wp-json", timeout: float = 60):
        self.url = url.rstrip("/")
        self.rest_prefix = rest_prefix
        token = base64.b64encode(f"{username}:{app_password}".encode()).decode()
        self._client = httpx.Client(headers={"Authorization": f"Basic {token}", "User-Agent": "cimage-ai-publisher/1.0"}, timeout=timeout,
                                    follow_redirects=True, transport=TRANSPORT)

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ plumbing
    def _endpoint(self, path: str) -> str:
        path = "/" + path.lstrip("/")
        if self.rest_prefix.startswith("/?rest_route="):
            return f"{self.url}/?rest_route={path}"
        return f"{self.url}{self.rest_prefix}{path}"

    def _request(self, method: str, path: str, **kw) -> Any:
        try:
            r = self._client.request(method, self._endpoint(path), **kw)
        except httpx.HTTPError as exc:
            raise WordPressError(f"cannot reach {self.url}: {exc}") from exc
        if r.status_code >= 400:
            code = msg = None
            try:
                body = r.json()
                code, msg = body.get("code"), body.get("message")
            except ValueError:
                pass
            if r.status_code in (401, 403):
                raise WordPressError(f"WordPress refused the credentials ({r.status_code}): {msg or 'check the username and application password'}", status=r.status_code, code=code)
            raise WordPressError(f"WordPress {method} {path} -> {r.status_code}: {msg or r.text[:200]}", status=r.status_code, code=code)
        if r.status_code == 204 or not r.content:
            return None
        try:
            return r.json()
        except ValueError as exc:
            raise WordPressError(f"WordPress returned non-JSON for {path} (REST API disabled or a security plugin in the way?)") from exc

    # ------------------------------------------------------------------ discovery / test
    def detect_rest_prefix(self) -> str:
        """Pretty permalinks give /wp-json; otherwise the site needs ?rest_route=."""
        for prefix in ("/wp-json", "/?rest_route="):
            self.rest_prefix = prefix
            try:
                r = self._client.get(self._endpoint("/"))
                if r.status_code < 400 and "application/json" in r.headers.get("content-type", ""):
                    return prefix
            except httpx.HTTPError:
                continue
        raise WordPressError(f"no WordPress REST API found at {self.url} (tried /wp-json and ?rest_route=)")

    def test(self) -> dict[str, Any]:
        prefix = self.detect_rest_prefix()
        me = self._request("GET", "/wp/v2/users/me", params={"context": "edit"})
        caps = me.get("capabilities") or {}
        root = self._request("GET", "/")
        return {
            "rest_prefix": prefix, "site_title": root.get("name"), "user": me.get("name") or me.get("slug"), "user_id": me.get("id"),
            "can_publish": bool(caps.get("publish_posts")), "can_upload": bool(caps.get("upload_files")), "can_edit": bool(caps.get("edit_posts")),
        }

    # ------------------------------------------------------------------ media
    def upload_media(self, data: bytes, filename: str, mime: str, *, title: str = "", alt: str = "", caption: str = "") -> dict[str, Any]:
        m = self._request("POST", "/wp/v2/media", content=data,
                          headers={"Content-Disposition": f'attachment; filename="{filename}"', "Content-Type": mime})
        meta = {k: v for k, v in {"title": title, "alt_text": alt, "caption": caption}.items() if v}
        if meta:
            try:
                m = self._request("POST", f"/wp/v2/media/{m['id']}", json=meta)
            except WordPressError as exc:   # the file is up; metadata is a nicety
                log.warning("could not set media metadata on %s: %s", m.get("id"), exc)
        return {"id": m["id"], "url": m.get("source_url") or (m.get("guid") or {}).get("rendered"), "title": title}

    # ------------------------------------------------------------------ taxonomies
    def _ensure_term(self, taxonomy: str, name: str) -> int | None:
        name = name.strip()
        if not name:
            return None
        found = self._request("GET", f"/wp/v2/{taxonomy}", params={"search": name, "per_page": 100})
        for t in found or []:
            if t.get("name", "").strip().lower() == name.lower():
                return int(t["id"])
        try:
            created = self._request("POST", f"/wp/v2/{taxonomy}", json={"name": name})
            return int(created["id"])
        except WordPressError as exc:
            if exc.code == "term_exists":   # race or a slug collision: WP tells us the id in the message body
                found = self._request("GET", f"/wp/v2/{taxonomy}", params={"search": name, "per_page": 100})
                return int(found[0]["id"]) if found else None
            raise

    def ensure_tags(self, names: list[str]) -> list[int]:
        ids: list[int] = []
        for n in dict.fromkeys(x.strip() for x in names if x and x.strip()):
            tid = self._ensure_term("tags", n)
            if tid and tid not in ids:
                ids.append(tid)
        return ids

    def ensure_category(self, name: str | None) -> int | None:
        return self._ensure_term("categories", name) if name else None

    # ------------------------------------------------------------------ posts
    def save_post(self, *, post_id: str | None, title: str, content: str, status: str, slug: str | None, excerpt: str | None,
                  featured_media: int | None, tags: list[int], categories: list[int], meta: dict[str, Any] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"title": title, "content": content, "status": status, "tags": tags, "categories": categories}
        if slug:
            body["slug"] = slug
        if excerpt:
            body["excerpt"] = excerpt
        if featured_media:
            body["featured_media"] = featured_media
        if meta:
            body["meta"] = meta
        path = f"/wp/v2/posts/{post_id}" if post_id else "/wp/v2/posts"
        try:
            p = self._request("POST", path, json=body)
        except WordPressError as exc:
            if meta and exc.status == 400 and "meta" in str(exc).lower():   # SEO plugin fields not registered on this site
                body.pop("meta")
                p = self._request("POST", path, json=body)
            else:
                raise
        return {"id": str(p["id"]), "url": p.get("link"), "status": p.get("status"), "edit_url": f"{self.url}/wp-admin/post.php?post={p['id']}&action=edit"}

    def get_post(self, post_id: str) -> dict[str, Any] | None:
        try:
            return self._request("GET", f"/wp/v2/posts/{post_id}", params={"context": "edit"})
        except WordPressError as exc:
            if exc.status == 404:
                return None
            raise
