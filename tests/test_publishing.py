"""Publishing: Markdown -> blocks, the WordPress client against a fake site, the publisher, and the API (approval -> auto-publish)."""
from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path

import httpx
import pytest
from PIL import Image

from services.publishing import wordpress
from services.publishing.crypto import SecretBox
from services.publishing.markdown_blocks import UploadedImage, markdown_to_blocks, plain_excerpt
from services.publishing.wordpress import WordPressClient, WordPressError
from tests.test_content import _indexed_job, pg_app  # noqa: F401 - fixtures
from tests.test_pg_store import store, test_db_url  # noqa: F401 - fixtures


class FakeWordPress:
    """Enough of the WP REST API for the publisher: auth, media, tags/categories, posts. Pretty permalinks (/wp-json)."""

    def __init__(self, user="editor", password="abcd efgh ijkl mnop qrst uvwx", *, fail_posts=False):
        self.auth = base64.b64encode(f"{user}:{password.replace(' ', '')}".encode()).decode()
        self.media, self.posts, self.tags, self.cats = {}, {}, {}, {}
        self.uploads = 0
        self.fail_posts = fail_posts
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, r: httpx.Request) -> httpx.Response:
        path = r.url.path
        if r.url.host != "site.test" or not path.startswith("/wp-json"):
            return httpx.Response(404, text="<html>not found</html>")
        p = path[len("/wp-json"):] or "/"
        if p == "/":
            return httpx.Response(200, json={"name": "CIMAGE Test Site", "namespaces": ["wp/v2"]})
        if r.headers.get("authorization") != f"Basic {self.auth}":
            return httpx.Response(401, json={"code": "rest_not_logged_in", "message": "bad credentials"})
        body = json.loads(r.content) if r.content and r.headers.get("content-type", "").startswith("application/json") else {}
        if p == "/wp/v2/users/me":
            return httpx.Response(200, json={"id": 3, "name": "Editor", "slug": "editor", "capabilities": {"publish_posts": True, "upload_files": True, "edit_posts": True}})
        if p == "/wp/v2/media" and r.method == "POST":
            self.uploads += 1
            mid = 100 + self.uploads
            self.media[mid] = {"id": mid, "source_url": f"https://site.test/wp-content/uploads/{mid}.jpg", "bytes": len(r.content)}
            return httpx.Response(201, json=self.media[mid])
        if p.startswith("/wp/v2/media/") and r.method == "POST":
            mid = int(p.rsplit("/", 1)[1]); self.media[mid].update(body); return httpx.Response(200, json=self.media[mid])
        for tax, table in (("tags", self.tags), ("categories", self.cats)):
            if p == f"/wp/v2/{tax}":
                if r.method == "GET":
                    q = (r.url.params.get("search") or "").lower()
                    return httpx.Response(200, json=[t for t in table.values() if q in t["name"].lower()])
                tid = len(table) + 1
                table[tid] = {"id": tid, "name": body["name"]}
                return httpx.Response(201, json=table[tid])
        if p == "/wp/v2/posts" or p.startswith("/wp/v2/posts/"):
            if self.fail_posts:
                return httpx.Response(500, json={"code": "boom", "message": "database error"})
            if r.method == "POST":
                pid = int(p.rsplit("/", 1)[1]) if p != "/wp/v2/posts" else len(self.posts) + 1
                post = self.posts.get(pid, {"id": pid})
                post.update(body); post["link"] = f"https://site.test/{post.get('slug') or 'p'}/"
                self.posts[pid] = post
                return httpx.Response(200 if pid in self.posts else 201, json=post)
            pid = int(p.rsplit("/", 1)[1])
            return httpx.Response(200, json=self.posts[pid]) if pid in self.posts else httpx.Response(404, json={"code": "rest_post_invalid_id", "message": "no"})
        return httpx.Response(404, json={"code": "rest_no_route", "message": p})


@pytest.fixture
def fake_wp(monkeypatch):
    wp = FakeWordPress()
    monkeypatch.setattr(wordpress, "TRANSPORT", wp.transport)
    return wp


def test_markdown_to_blocks_house_style():
    md = "Intro.\n\nMore intro.\n\n## A\n\nText A1.\n\nText A2.\n\n[img=a]\n\n[img=a2]\n\n## B\n\nText B.\n"
    pics = {"a": UploadedImage(11, "https://s/a.jpg", "alt a", "Cap A"), "a2": UploadedImage(12, "https://s/a2.jpg", "alt a2", "")}
    out = markdown_to_blocks(md, pics.get, hero=UploadedImage(10, "https://s/hero.jpg", "hero", "Hero cap"))
    order = [(m.group(1) or m.group(2) or m.group(3))[:14] for m in re.finditer(r'<(h2)[^>]*>|<img src="([^"]+)"|<p>([^<]{0,14})', out)]
    # hero after the intro paragraph; the section's first picture hoisted under its heading; the second stays where it was; no captions
    assert order == ["Intro.", "https://s/hero", "More intro.", "h2", "https://s/a.jp", "Text A1.", "Text A2.", "https://s/a2.j", "h2", "Text B."]
    assert "figcaption" not in out
    kept = markdown_to_blocks(md, pics.get, image_position="as_placed", captions=True)
    assert "<figcaption" in kept and kept.index("Text A2.") < kept.index("https://s/a.jpg") and "hero" not in kept


def test_markdown_to_blocks_and_excerpt():
    md = "Intro **bold** [id=j:q:0].\n\n## Section\n\n* one\n* two\n\n[img=abc]\n\n[img=ghost]\n\n> quote\n\n**Q?**\nAnswer [id=x, id=y].\n"
    out = markdown_to_blocks(md, lambda i: UploadedImage(12, "https://s/x.jpg", "alt", "Cap") if i == "abc" else None, image_position="as_placed", captions=True)
    assert out.count("<!-- wp:paragraph -->") == 4 and '<!-- wp:heading {"level":2} -->' in out and out.count("<!-- wp:list-item -->") == 2
    assert 'class="wp-image-12"' in out and "Cap" in out and "ghost" not in out and "[id=" not in out and "<strong>bold</strong>" in out
    assert "<!-- wp:quote -->" in out and "<p>Answer.</p>" in out
    assert plain_excerpt("a " * 200, 50).endswith("…") and plain_excerpt("short [id=x]") == "short"


def test_secret_box_roundtrip_and_keyfile(tmp_path: Path):
    kf = tmp_path / ".secret_key"
    box = SecretBox(key_file=kf)
    assert kf.exists() and (kf.stat().st_mode & 0o777) == 0o600
    tok = box.encrypt("abcd efgh")
    assert tok != "abcd efgh" and SecretBox(key_file=kf).decrypt(tok) == "abcd efgh"
    with pytest.raises(RuntimeError):
        SecretBox(key=SecretBox(key_file=tmp_path / "other").encrypt("x")[:0] or __import__("cryptography").fernet.Fernet.generate_key()).decrypt(tok)


def test_wordpress_client_against_fake_site(fake_wp):
    c = WordPressClient("https://site.test/", "editor", "abcd efgh ijkl mnop qrst uvwx".replace(" ", ""))
    info = c.test()
    assert info["rest_prefix"] == "/wp-json" and info["site_title"] == "CIMAGE Test Site" and info["can_publish"]
    m = c.upload_media(b"\xff\xd8jpegbytes", "hero.jpg", "image/jpeg", title="Hero", alt="campus", caption="The campus")
    assert m["id"] == 101 and m["url"].endswith("/101.jpg") and fake_wp.media[101]["alt_text"] == "campus"
    assert c.ensure_tags(["CIMAGE Patna", "cimage patna", "BCA"]) == [1, 2] and c.ensure_tags(["BCA"]) == [2]
    assert c.ensure_category("News") == 1 and c.ensure_category(None) is None
    p = c.save_post(post_id=None, title="T", content="<p>x</p>", status="draft", slug="t", excerpt="e", featured_media=101, tags=[1], categories=[1], meta={"_yoast_wpseo_title": "seo"})
    assert p["id"] == "1" and p["status"] == "draft" and p["url"] == "https://site.test/t/" and "post=1" in p["edit_url"]
    p2 = c.save_post(post_id="1", title="T2", content="<p>y</p>", status="publish", slug="t", excerpt="e", featured_media=101, tags=[1], categories=[])
    assert p2["id"] == "1" and p2["status"] == "publish" and fake_wp.posts[1]["title"] == "T2"
    assert c.get_post("1")["title"] == "T2" and c.get_post("99") is None
    bad = WordPressClient("https://site.test", "editor", "wrong")
    with pytest.raises(WordPressError) as exc:
        bad.test()
    assert "credentials" in str(exc.value) and exc.value.status == 401
    with pytest.raises(WordPressError):
        WordPressClient("https://nowhere.test", "e", "p").detect_rest_prefix()


def _finished_draft(store, content, images, tiny_video, tmp_path):  # noqa: F811
    from tests.test_content import _indexed_job

    job = _indexed_job(store, tiny_video)
    d = content.create_draft("Campus article", None, depth="in_depth")
    photo = tmp_path / "hero.jpg"
    Image.new("RGB", (640, 360), (20, 60, 120)).save(photo)
    hero = images.add_library_file(photo, description="Campus at dusk", tags=["campus"])
    photo2 = tmp_path / "lab.jpg"
    Image.new("RGB", (640, 360), (120, 60, 20)).save(photo2)
    lab = images.add_library_file(photo2, description="Robotics lab", tags=["lab"])
    body = "We are proud [id=%s:summary:0].\n\n## The lab\n\nStudents build robots.\n\n[img=%s]\n\n## Key Takeaways\n\n* one\n* two\n" % (job.id, lab.id)
    d = content.finish_draft(d.id, title="Inside the CIMAGE robotics lab", slug="inside-the-cimage-robotics-lab", body_markdown=body,
                             seo={"seo_title": "Robotics lab | CIMAGE Patna", "meta_description": "desc", "excerpt": "A look inside.", "tags": ["CIMAGE Patna", "Robotics"]},
                             social={}, citations=[{"block_id": f"{job.id}:summary:0", "used_for": "x"}], evidence={"job_id": job.id, "blocks": []}, hero_block_id=None,
                             model="m", prompt_version="blog_v2", usage={}, warnings=[],
                             images=[{"image_id": hero.id, "placement": "hero", "caption": "The campus", "alt_text": "campus at dusk"},
                                     {"image_id": lab.id, "placement": "inline", "caption": "The lab", "alt_text": "robotics lab"}])
    return content.set_draft_status(d.id, "approved"), hero, lab


def test_store_and_publisher_end_to_end(store, tiny_video, tmp_path, fake_wp):  # noqa: F811
    from apps.api.content_store import ContentStore
    from services.media_library.store import ImageStore
    from services.publishing.publisher import PublishError, publish_draft, test_target
    from services.publishing.store import PublishStore, Target

    content = ContentStore(store.pool)
    images = ImageStore(store.pool, tmp_path / "images")
    ps = PublishStore(store.pool, SecretBox(key_file=tmp_path / ".key"))
    t = ps.create_target(Target(name="cimage.in", url="https://site.test/", username="editor", mode="draft", default_category="News", seo_plugin="yoast"), "abcdefghijklmnopqrstuvwx")
    assert t.url == "https://site.test" and t.has_secret and not hasattr(t, "secret_enc") and ps.secret_for(t.id) == "abcdefghijklmnopqrstuvwx"
    with store.pool.connection() as conn:
        raw = conn.execute("SELECT secret_enc FROM publish_targets WHERE id = %s", (t.id,)).fetchone()["secret_enc"]
    assert "abcdefghijklmnopqrstuvwx" not in raw
    info = test_target(ps, t)
    assert info["site_title"] == "CIMAGE Test Site" and ps.get_target(t.id).last_test_ok and ps.get_target(t.id).site_title == "CIMAGE Test Site"

    draft, hero, lab = _finished_draft(store, content, images, tiny_video, tmp_path)
    pub = publish_draft(draft, t, ps, images, triggered_by="approval")
    assert pub.status == "draft" and pub.remote_id == "1" and pub.remote_url == "https://site.test/inside-the-cimage-robotics-lab/" and pub.draft_version == draft.version
    assert set(pub.media_map) == {hero.id, lab.id} and fake_wp.uploads == 2 and pub.detail["tags"] == 2 and pub.detail["category"] == 1
    post = fake_wp.posts[1]
    assert post["status"] == "draft" and post["featured_media"] == pub.media_map[hero.id]["id"] and post["categories"] == [1] and post["slug"] == "inside-the-cimage-robotics-lab"
    assert 'class="wp-image-%d"' % pub.media_map[lab.id]["id"] in post["content"] and "[img=" not in post["content"] and "[id=" not in post["content"]
    assert 'class="wp-image-%d"' % pub.media_map[hero.id]["id"] in post["content"]        # house style: the lead image is also in the body
    assert post["content"].index("<h2") < post["content"].index('wp-image-%d' % pub.media_map[lab.id]["id"]) < post["content"].index("Students build robots")   # hoisted under the heading
    assert "figcaption" not in post["content"] and t.layout.image_position == "under_heading"
    assert post["meta"] == {"_yoast_wpseo_title": "Robotics lab | CIMAGE Patna", "_yoast_wpseo_metadesc": "desc"} and post["excerpt"] == "A look inside."
    assert fake_wp.media[pub.media_map[hero.id]["id"]]["alt_text"] == "campus at dusk"

    # re-publish live: same post updated, pictures not uploaded again
    pub2 = publish_draft(draft, t, ps, images, mode="publish")
    assert pub2.id == pub.id and pub2.status == "published" and pub2.remote_id == "1" and fake_wp.uploads == 2 and fake_wp.posts[1]["status"] == "publish"
    assert [p.id for p in ps.list_publications(draft_id=draft.id)] == [pub.id]

    # a failing site leaves a failed row, never an exception
    fake_wp.fail_posts = True
    pub3 = publish_draft(draft, t, ps, images)
    assert pub3.status == "failed" and "database error" in pub3.error
    bad = ps.update_target(t.id, secret="nope")
    with pytest.raises(PublishError):
        test_target(ps, bad)
    assert ps.get_target(t.id).last_test_ok is False and ps.delete_target(t.id) and ps.get_target(t.id) is None


def test_publishing_api_and_auto_publish_on_approval(pg_app, tiny_video, fake_wp):  # noqa: F811
    client = pg_app
    r = client.post("/api/v1/publish/targets", json={"name": "cimage.in", "url": "https://site.test", "username": "editor", "app_password": "abcd efgh ijkl mnop qrst uvwx", "mode": "draft"})
    assert r.status_code == 201, r.text
    t = r.json()
    assert "secret_enc" not in t and "abcd" not in json.dumps(t) and t["has_secret"] and t["auto_on_approval"]
    assert client.post("/api/v1/publish/targets", json={"name": "x", "url": "https://site.test", "username": "e"}).status_code == 400
    assert client.post(f"/api/v1/publish/targets/{t['id']}/test").json()["site_title"] == "CIMAGE Test Site"
    assert client.get("/api/v1/publish/targets").json()[0]["last_test_ok"] is True

    with tiny_video.open("rb") as f:
        job_id = client.post("/api/v1/analyze", files={"file": (tiny_video.name, f, "video/mp4")}).json()["job_id"]
    for _ in range(100):
        if client.get(f"/api/v1/jobs/{job_id}").json()["state"] in ("CONTENT_CANDIDATE", "INDEXED", "FAILED"):
            break
        time.sleep(0.1)
    d = client.post("/api/v1/drafts", json={"brief": "Article about the lab", "job_id": job_id, "depth": "standard"}).json()
    for _ in range(100):
        d = client.get(f"/api/v1/drafts/{d['id']}").json()
        if d["status"] != "generating":
            break
        time.sleep(0.1)
    assert d["status"] == "new"
    assert client.post(f"/api/v1/drafts/{d['id']}/publish", json={}).status_code == 409   # not approved yet

    assert client.post(f"/api/v1/drafts/{d['id']}/status", json={"status": "approved"}).json()["status"] == "approved"
    for _ in range(100):
        pubs = client.get(f"/api/v1/drafts/{d['id']}/publications").json()
        if pubs and pubs[0]["status"] not in ("queued", "publishing"):
            break
        time.sleep(0.1)
    assert len(pubs) == 1 and pubs[0]["status"] == "draft" and pubs[0]["triggered_by"] == "approval" and pubs[0]["target_name"] == "cimage.in"
    assert pubs[0]["remote_url"].startswith("https://site.test/") and pubs[0]["edit_url"].endswith("&action=edit")
    assert fake_wp.posts[int(pubs[0]["remote_id"])]["status"] == "draft"

    r = client.post(f"/api/v1/drafts/{d['id']}/publish", json={"target_ids": [t["id"]], "mode": "publish"})
    assert r.status_code == 202 and r.json()[0]["status"] == "queued"
    for _ in range(100):
        pubs = client.get(f"/api/v1/drafts/{d['id']}/publications").json()
        if pubs[0]["status"] == "published":
            break
        time.sleep(0.1)
    assert pubs[0]["status"] == "published" and pubs[0]["remote_id"] == pubs[0]["remote_id"] and fake_wp.posts[int(pubs[0]["remote_id"])]["status"] == "publish"
    assert client.get("/api/v1/publish/publications").json()[0]["draft_id"] == d["id"]
    upd = client.put(f"/api/v1/publish/targets/{t['id']}", json={"mode": "publish", "auto_on_approval": False, "layout": {"image_captions": True, "image_position": "as_placed"}}).json()
    assert upd["mode"] == "publish" and upd["layout"] == {"hero_in_body": True, "image_position": "as_placed", "image_captions": True}
    assert client.delete(f"/api/v1/publish/targets/{t['id']}").json()["deleted"] == t["id"]
    assert client.get("/api/v1/publish/targets").json() == []
