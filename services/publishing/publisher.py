"""Orchestration: an approved draft -> one WordPress target. Uploads the pictures it needs (reusing earlier uploads),
creates tags / the category, converts the article to block markup and creates or updates the post."""
from __future__ import annotations

import logging
import mimetypes
import re
import time
from pathlib import Path
from typing import Any

from services.publishing.markdown_blocks import UploadedImage, markdown_to_blocks, plain_excerpt
from services.publishing.store import Publication, PublishStore, Target
from services.publishing.wordpress import WordPressClient, WordPressError

log = logging.getLogger(__name__)


class PublishError(RuntimeError):
    pass


def client_for(store: PublishStore, target: Target) -> WordPressClient:
    return WordPressClient(target.url, target.username, store.secret_for(target.id), rest_prefix=target.rest_prefix)


def test_target(store: PublishStore, target: Target) -> dict[str, Any]:
    """Connection test; records the outcome (and the REST prefix / site title it discovered) on the target."""
    client = client_for(store, target)
    try:
        info = client.test()
    except WordPressError as exc:
        store.update_target(target.id, last_test_at=_now(), last_test_ok=False, last_error=str(exc)[:500])
        raise PublishError(str(exc)) from exc
    finally:
        client.close()
    store.update_target(target.id, last_test_at=_now(), last_test_ok=True, last_error=None, rest_prefix=info["rest_prefix"], site_title=info.get("site_title"))
    return info


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _seo_meta(target: Target, seo: dict[str, Any]) -> dict[str, Any] | None:
    title, desc = seo.get("seo_title"), seo.get("meta_description")
    if target.seo_plugin == "yoast":
        return {k: v for k, v in {"_yoast_wpseo_title": title, "_yoast_wpseo_metadesc": desc}.items() if v}
    if target.seo_plugin == "rankmath":
        return {k: v for k, v in {"rank_math_title": title, "rank_math_description": desc}.items() if v}
    return None


def publish_draft(draft, target: Target, store: PublishStore, images, *, mode: str | None = None, triggered_by: str = "editor") -> Publication:
    """Publish (or re-publish) `draft` to `target`. `images` is the ImageStore (for the picture files). Never raises: the
    publication row carries the outcome."""
    mode = mode or target.mode
    pub = store.upsert_publication(draft.id, target.id, mode=mode, triggered_by=triggered_by)
    store.update_publication(pub.id, status="publishing")
    started = time.monotonic()
    client = client_for(store, target)
    try:
        media_map: dict[str, Any] = dict(pub.media_map or {})
        placements = {im["image_id"]: im for im in (draft.images or []) if im.get("image_id")}
        recs = images.get_many(list(placements)) if placements and images is not None else {}

        def uploaded(image_id: str) -> UploadedImage | None:
            pl = placements.get(image_id)
            rec = recs.get(image_id)
            if pl is None or rec is None or not Path(rec.path).exists():
                return None
            if image_id not in media_map:
                p = Path(rec.path)
                mime = mimetypes.guess_type(p.name)[0] or "image/jpeg"
                fname = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{draft.slug or 'article'}-{image_id}{p.suffix.lower()}")
                m = client.upload_media(p.read_bytes(), fname, mime, title=pl.get("alt_text") or pl.get("caption") or draft.title or "", alt=pl.get("alt_text") or "", caption=pl.get("caption") or "")
                media_map[image_id] = {"id": m["id"], "url": m["url"]}
                store.update_publication(pub.id, media_map=media_map)   # survive a later failure without re-uploading
            m = media_map[image_id]
            return UploadedImage(int(m["id"]), m["url"], alt=pl.get("alt_text") or "", caption=pl.get("caption") or "")

        hero = next((im for im in (draft.images or []) if im.get("placement") == "hero"), None)
        featured = uploaded(hero["image_id"]) if hero else None
        lay = target.layout
        content = markdown_to_blocks(draft.body_markdown or "", uploaded, hero=featured if lay.hero_in_body else None,
                                     image_position=lay.image_position, captions=lay.image_captions)
        seo = draft.seo or {}
        tags = client.ensure_tags(list(seo.get("tags") or []))
        cat = client.ensure_category(target.default_category)
        post = client.save_post(
            post_id=pub.remote_id, title=draft.title or draft.brief, content=content, status="publish" if mode == "publish" else "draft",
            slug=draft.slug, excerpt=seo.get("excerpt") or plain_excerpt(draft.body_markdown or ""), featured_media=featured.media_id if featured else None,
            tags=tags, categories=[cat] if cat else [], meta=_seo_meta(target, seo),
        )
        status = "published" if post.get("status") == "publish" else "draft"
        return store.update_publication(
            pub.id, status=status, remote_id=post["id"], remote_url=post.get("url"), edit_url=post.get("edit_url"), draft_version=draft.version,
            media_map=media_map, error=None,
            detail={"seconds": round(time.monotonic() - started, 2), "tags": len(tags), "category": cat, "pictures": len(media_map), "wp_status": post.get("status")},
        )
    except (WordPressError, PublishError, OSError, KeyError, ValueError) as exc:
        log.warning("publishing draft %s to %s failed: %s", draft.id, target.name, exc)
        return store.update_publication(pub.id, status="failed", error=str(exc)[:1000])
    finally:
        client.close()
