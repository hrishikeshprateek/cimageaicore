"""Pictures for content: video frames, the local library, the writer's image placements. Postgres parts skip without a server."""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest
from PIL import Image

from services.media_library.frames import extract_frame, sharpness
from tests.test_content import _indexed_job, pg_app  # noqa: F401 - fixtures
from tests.test_pg_store import store, test_db_url  # noqa: F401 - fixtures

pytestmark_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")


@pytestmark_ffmpeg
def test_extract_frame_picks_the_sharpest_candidate_and_survives_bad_timestamps(tiny_video: Path, tmp_path: Path):
    out, w, h = extract_frame(tiny_video, 1.0, tmp_path / "f.jpg", duration=3.0)
    assert out.exists() and (w, h) == (320, 240) and sharpness(out) > 0
    late, _, _ = extract_frame(tiny_video, 5.0, tmp_path / "late.jpg", duration=3.0)   # analysis timestamps can overshoot the file
    assert late.exists()
    with pytest.raises(Exception):
        extract_frame(tmp_path / "missing.mp4", 1.0, tmp_path / "x.jpg")


def _fake_provider(payload: dict):
    from services.ai_gateway.base import RawModelOutput

    class P:
        name = "fake"
        model = "fake-1"

        def generate_structured(self, system_instruction, prompt, json_schema, *, model=None, thinking_level=None):
            self.prompt = prompt
            return RawModelOutput(text=json.dumps(payload), model="fake-1")

        def repair_json(self, invalid_text, error, json_schema):
            return RawModelOutput(text=invalid_text, model="fake-1")

    return P()


class _Pic:
    def __init__(self, id, description, kind="frame", suitable_for=()):
        self.id, self.description, self.kind, self.suitable_for = id, description, kind, list(suitable_for)

    def offer_line(self):
        return f"- [img={self.id}] ({self.kind}) {self.description}"


def test_agent_grounds_image_markers_like_citations(store, tiny_video):  # noqa: F811
    from agents.blog_agent.agent import BlogAgent, image_marker_ids, render_image_markers
    from services.ai_gateway.embeddings import MockEmbedder
    from services.retrieval.retriever import Retriever

    job = _indexed_job(store, tiny_video)
    pics = [_Pic("imgA", "Director at his desk", suitable_for=["blog_hero"]), _Pic("imgB", "Students in the lab"), _Pic("imgC", "Campus gate", kind="library")]
    body = ("Intro para [id=%s:summary:0]. [img=imgB]\n\n## First\n\nText.\n\n[img=ghost]\n\n## Second\n\nMore text [img=imgA]\n\n## Third\n\nEnd.\n" % job.id)
    payload = {
        "title": "T", "slug": "t", "seo_title": "s", "meta_description": "m", "excerpt": "e", "body_markdown": body, "tags": ["a"],
        "citations": [{"block_id": f"{job.id}:summary:0", "used_for": "x"}], "hero_block_id": None,
        "social": {"linkedin": "l", "instagram": "i", "facebook": "f"}, "evidence_gaps": [],
        "images": [
            {"image_id": "imgA", "placement": "hero", "caption": "The Director", "alt_text": "man at desk"},
            {"image_id": "imgC", "placement": "inline", "caption": "Our gate", "alt_text": "gate"},          # listed but no marker -> placed automatically
            {"image_id": "nope", "placement": "inline", "caption": "?", "alt_text": "?"},                  # not offered -> dropped
        ],
    }
    provider = _fake_provider(payload)
    agent = BlogAgent(provider, Retriever(store, MockEmbedder(768)), prompt_version="blog_v2", institution_context="Test College")
    res = agent.draft("Lab article", job_id=job.id, depth="in_depth", images_for=lambda ev: pics)
    d = res.draft
    assert "AVAILABLE IMAGES" in provider.prompt and "[img=imgA]" in provider.prompt and "IN-DEPTH" in provider.prompt
    assert image_marker_ids(d.body_markdown) == ["imgB", "imgC"]            # ghost removed, hero not in body, imgC auto-placed before an H2
    assert "[img=ghost]" not in d.body_markdown and "\n[img=imgB]\n" in d.body_markdown  # marker lifted onto its own line
    assert [(p.image_id, p.placement) for p in d.images] == [("imgA", "hero"), ("imgB", "inline"), ("imgC", "inline")]
    assert next(p for p in d.images if p.image_id == "imgB").caption == "Students in the lab"   # marker without entry -> caption from the picture
    assert any("outside the offered pictures" in w for w in res.warnings) and any("placed automatically" in w for w in res.warnings)
    clean = render_image_markers(d.body_markdown, d.images)
    assert "![Students in the lab](/api/v1/images/imgB)" in clean and "*Our gate*" in clean and "[img=" not in clean
    assert res.depth == "in_depth" and res.offered_images == pics

    # no hero chosen and no markers at all -> best offered frame becomes the hero, nothing inline
    payload["images"], payload["body_markdown"] = [], "Intro.\n\n## One\n\nText.\n"
    res2 = BlogAgent(_fake_provider(payload), Retriever(store, MockEmbedder(768)), prompt_version="blog_v2").draft("x", job_id=job.id, images_for=lambda ev: pics)
    assert [(p.image_id, p.placement) for p in res2.draft.images] == [("imgA", "hero")] and any("best offered picture" in w for w in res2.warnings)
    p3 = _fake_provider(payload)
    res3 = BlogAgent(p3, Retriever(store, MockEmbedder(768)), prompt_version="blog_v2").draft("x", job_id=job.id)
    assert res3.draft.images == [] and "(no pictures available" in p3.prompt


@pytestmark_ffmpeg
def test_shot_index_with_mock_vision(tiny_video: Path, tmp_path: Path):
    from services.ai_gateway.mock import MockProvider
    from services.media_library.vision import ahash, contact_sheet, detect_shots, index_shots, youtube_video_id

    times = detect_shots(tiny_video, duration=3.0)
    assert times and times[0] == 0.5 and all(0 <= t < 3 for t in times)
    stills = []
    for i, t in enumerate(times[:4]):
        p = tmp_path / f"s{i}.jpg"
        extract_frame(tiny_video, t, p, duration=3.0, offsets=(0.0,))
        stills.append((i + 1, t, p))
    sheet = contact_sheet(stills, tmp_path / "sheet.jpg")
    with Image.open(sheet) as im:
        assert im.width == 1800 and im.height in (338, 676)
    assert ahash(stills[0][2]) == ahash(stills[0][2])

    class B:  # a media block near the first still
        block_type, timestamp, block_id, text = "media", "00:00:01", "j:media:0", "Opening frame"

    shots, meta = index_shots(tiny_video, duration=3.0, source_name="clip.mp4", blocks=[B()], provider=MockProvider(delay_seconds=0))
    assert meta["sheets"] >= 1 and shots and shots[0].quality == "good" and shots[0].block_id == "j:media:0" and shots[0].description.startswith("[MOCK]")
    assert any(sh.quality == "transition" for sh in shots) or len(shots) < 3
    assert youtube_video_id("https://www.youtube.com/watch?v=jZlzKdq0StM&t=1") == "jZlzKdq0StM" and youtube_video_id("https://example.com/x") is None


@pytestmark_ffmpeg
def test_image_store_frames_and_library(store, tiny_video, tmp_path):  # noqa: F811
    from services.media_library.store import ImageStore

    images = ImageStore(store.pool, tmp_path / "images")
    job = _indexed_job(store, tiny_video)
    frames = images.frames_for_job(job, store.blocks(job.id))
    assert frames and {f.block_id.split(":")[1] for f in frames} <= {"media", "key_moment"}
    media = next(f for f in frames if ":media:" in f.block_id)
    assert media.kind == "frame" and media.description.startswith("[MOCK]") and "thumbnail" in media.suitable_for and Path(media.path).exists()
    assert media.timestamp == "00:00:05" and media.width == 320
    again = images.frames_for_job(job, store.blocks(job.id))
    assert [f.id for f in again] == [f.id for f in frames]                       # idempotent
    forced = images.frames_for_job(job, store.blocks(job.id), force=True)
    assert [f.id for f in forced] != [f.id for f in frames] and len(images.list(job_id=job.id)) == len(frames)
    at = images.frame_at(job, 1.2, description="editor pick")
    assert at.timestamp_seconds == 1.2 and at.tags == ["editor"] and Path(at.path).exists()

    from services.ai_gateway.mock import MockProvider

    ai, meta = images.index_job(job, store.blocks(job.id), MockProvider(delay_seconds=0))
    assert ai and meta["stored"] == len(ai) and all("ai" in r.tags and r.description.startswith("[MOCK] still") for r in ai)
    matched = [r for r in ai if r.block_id]
    assert matched and matched[0].block_id.startswith(job.id + ":")                 # the AI still took over that block ...
    assert not any(r.block_id == matched[0].block_id and "blocks" in r.tags for r in images.list(job_id=job.id))   # ... from the timestamp-based frame
    cached, meta2 = images.index_job(job, store.blocks(job.id), MockProvider(delay_seconds=0))
    assert meta2.get("cached") and [r.id for r in cached] == [r.id for r in ai]
    offered = images.offers(job_ids=[job.id], brief="x")
    assert all("blocks" not in r.tags for r in offered if r.kind == "frame")      # unverified frames are no longer offered
    assert offered[0].id == next(r.id for r in ai if "blog_hero" in r.suitable_for)

    photo = tmp_path / "campus gate.png"
    Image.new("RGB", (800, 600), (200, 30, 30)).save(photo)
    lib = images.add_library_file(photo, description="Main gate of the campus", tags=["campus", "gate"])
    assert lib.kind == "library" and lib.sha256 and lib.width == 800 and Path(lib.path).name.endswith("campus_gate.png")
    assert images.add_library_file(photo, description="dup").id == lib.id           # same bytes -> same record
    folder = tmp_path / "photos"
    folder.mkdir()
    Image.new("RGB", (640, 480), (20, 200, 30)).save(folder / "robotics_lab.jpg")
    Image.new("RGB", (640, 480), (20, 30, 200)).save(folder / "seminar_hall.jpg")
    (folder / "captions.txt").write_text("seminar_hall.jpg\tSeminar hall during the AI workshop\n", encoding="utf-8")
    imported = images.import_folder(folder, tags=["2026"])
    assert {i.description for i in imported} == {"robotics lab", "Seminar hall during the AI workshop"} and all("photos" in i.tags for i in imported)
    offers = images.offers(job_ids=[job.id], brief="Our robotics lab and the workshop")
    assert offers[0].kind == "frame" and [o.description for o in offers if o.kind == "library"][:2] == ["robotics lab", "Seminar hall during the AI workshop"]
    assert images.update(lib.id, description="Gate", tags=["x"]).description == "Gate"
    assert images.delete(lib.id) and images.get(lib.id) is None and not Path(lib.path).exists()


@pytestmark_ffmpeg
def test_frames_images_and_draft_pictures_through_the_api(pg_app, tiny_video):  # noqa: F811
    client = pg_app
    with tiny_video.open("rb") as f:
        job_id = client.post("/api/v1/analyze", files={"file": (tiny_video.name, f, "video/mp4")}).json()["job_id"]
    for _ in range(100):
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["state"] in ("CONTENT_CANDIDATE", "INDEXED", "FAILED"):
            break
        time.sleep(0.1)
    assert job["state"] != "FAILED"

    res = client.post(f"/api/v1/jobs/{job_id}/frames").json()          # mode=ai with the mock provider: shot detection + canned descriptions
    assert res["mode"] == "ai" and res["meta"]["sheets"] >= 1 and res["meta"]["stored"] == len(res["images"])
    frames = res["images"]
    assert frames and all(f["url"].startswith("/api/v1/images/") and "ai" in f["tags"] and f["description"].startswith("[MOCK] still") for f in frames)
    quick = client.post(f"/api/v1/jobs/{job_id}/frames?mode=blocks").json()
    assert quick["mode"] == "blocks" and all(("blocks" in f["tags"]) or ("ai" in f["tags"]) for f in quick["images"])   # a block already owned by an AI still keeps it
    assert client.post(f"/api/v1/jobs/{job_id}/frames").json()["meta"].get("cached") is True   # AI stills are not recomputed unless forced
    img = client.get(frames[0]["url"])
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
    assert client.get(f"/api/v1/images?job_id={job_id}").json()[0]["kind"] == "frame"
    assert client.post(f"/api/v1/jobs/{job_id}/frames/at?at=1.5&description=custom").json()["description"] == "custom"
    assert client.patch(f"/api/v1/images/{frames[0]['id']}", json={"description": "Opening frame, edited", "tags": ["opening"]}).json()["tags"] == ["opening"]

    photo = Path(tiny_video).parent / "gate.jpg"
    Image.new("RGB", (400, 300), (10, 10, 120)).save(photo)
    with photo.open("rb") as f:
        lib = client.post("/api/v1/images/library", files={"file": ("gate.jpg", f, "image/jpeg")}, data={"description": "Campus gate", "tags": "campus, gate"})
    assert lib.status_code == 201 and lib.json()["kind"] == "library" and lib.json()["tags"] == ["campus", "gate"]
    assert client.post("/api/v1/images/library/import", json={"path": "/definitely/not/here"}).status_code == 404

    d = client.post("/api/v1/drafts", json={"brief": "Article about our campus gate and lab", "job_id": job_id, "depth": "in_depth"}).json()
    assert d["status"] == "generating" and d["depth"] == "in_depth"
    for _ in range(100):
        d = client.get(f"/api/v1/drafts/{d['id']}").json()
        if d["status"] != "generating":
            break
        time.sleep(0.1)
    assert d["status"] == "new", d.get("error")
    # the mock writer places no pictures itself -> the best offered frame became the hero
    assert d["images"] and d["images"][0]["placement"] == "hero" and d["hero_image_id"] == d["images"][0]["image_id"]
    assert d["evidence"]["offered_images"] and any("best offered picture" in w for w in d["warnings"])
    # the editor adds the library photo inline
    body = {"images": [d["images"][0], {"image_id": lib.json()["id"], "placement": "inline", "caption": "The gate", "alt_text": "gate"}]}
    r = client.put(f"/api/v1/drafts/{d['id']}/images", json=body)
    assert r.status_code == 200, r.text
    d2 = r.json()
    assert d2["version"] == d["version"] + 1 and f"[img={lib.json()['id']}]" in d2["body_markdown"] and "![gate](/api/v1/images/" in d2["body_markdown_clean"]
    assert client.put(f"/api/v1/drafts/{d['id']}/images", json={"images": [{"image_id": "zzz", "placement": "inline", "caption": "", "alt_text": ""}]}).status_code == 400
    assert client.delete(f"/api/v1/images/{lib.json()['id']}").json()["deleted"]
