"""Video Composer through the API: mock-provider analysis -> proposed cuts -> compose -> render -> stream. Offline."""
from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")


@pytest.fixture
def composer_env(app_env, tmp_path, monkeypatch):
    monkeypatch.setenv("COMPOSER_ENABLED", "true")
    monkeypatch.setenv("COMPOSER_TEMPLATES_DIR", str(tmp_path / "templates"))
    monkeypatch.setenv("COMPOSER_RENDERS_DIR", str(tmp_path / "renders"))
    monkeypatch.setenv("COMPOSER_X264_PRESET", "ultrafast")
    monkeypatch.setenv("COMPOSER_CRF", "30")
    monkeypatch.setenv("COMPOSER_MIN_CUT_SECONDS", "2")
    monkeypatch.setenv("COMPOSER_MAX_CUT_SECONDS", "6")
    monkeypatch.setenv("COMPOSER_TARGET_CUT_SECONDS", "4")
    from services.video_composer import settings as cs

    cs.get_composer_settings.cache_clear()
    yield {**app_env, "tmp": tmp_path}
    cs.get_composer_settings.cache_clear()


def _wait(client, path, done, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = client.get(path).json()
        if done(d):
            return d
        time.sleep(0.15)
    raise AssertionError(f"timed out waiting on {path}: {d}")


def _analysed_job(client, tiny_video: Path) -> str:
    with tiny_video.open("rb") as f:
        r = client.post("/api/v1/analyze", files={"file": (tiny_video.name, f, "video/mp4")})
    assert r.status_code == 202, r.text
    job = _wait(client, f"/api/v1/jobs/{r.json()['job_id']}", lambda j: j["state"] in ("BLOCKS_COMPLETE", "FAILED"))
    assert job["state"] == "BLOCKS_COMPLETE", job.get("error")
    return job["id"]


def test_disabled_flag_returns_503(app_env, monkeypatch):
    monkeypatch.setenv("COMPOSER_ENABLED", "false")
    from services.video_composer import settings as cs

    cs.get_composer_settings.cache_clear()
    from apps.api.main import create_app

    with TestClient(create_app()) as client:
        assert client.get("/api/v1/composer/system").json()["enabled"] is False
        assert client.get("/api/v1/composer/templates").status_code == 503
        assert client.get("/composer").status_code == 200   # page loads and explains the flag
    cs.get_composer_settings.cache_clear()


def test_cuts_compose_render_stream(composer_env, tiny_video: Path):
    from apps.api.main import create_app

    with TestClient(create_app()) as client:
        sysinfo = client.get("/api/v1/composer/system").json()
        assert sysinfo["enabled"] and sysinfo["render_store"] == "json" and [t["name"] for t in sysinfo["templates"]] == ["placeholder"]
        assert client.get("/api/v1/composer/templates/placeholder").json()["layouts"]["reels"]["width"] == 1080
        png = client.get("/api/v1/composer/templates/placeholder/preview?preset=reels&aspect=16:9")
        assert png.status_code == 200 and png.headers["content-type"] == "image/png" and png.content[:4] == b"\x89PNG"

        job_id = _analysed_job(client, tiny_video)
        assert client.get("/api/v1/jobs/nope/cuts").status_code == 404
        cuts = client.get(f"/api/v1/jobs/{job_id}/cuts").json()
        assert cuts["job_id"] == job_id and cuts["refined"] is False and cuts["cuts"], cuts
        c = cuts["cuts"][0]
        assert 0 <= c["in_seconds"] < c["out_seconds"] <= (cuts["duration_seconds"] or 3) + 0.01 and c["captions"]
        refined = client.get(f"/api/v1/jobs/{job_id}/cuts?refine=true").json()   # mock provider cannot refine -> rule-based with a warning
        assert refined["refined"] is False and "unavailable" in refined["warning"]
        cues = client.get(f"/api/v1/jobs/{job_id}/captions", params={"cut_in": 0, "cut_out": 2}).json()
        assert cues and cues[0]["start"] == 0
        media = client.get(f"/api/v1/jobs/{job_id}/media")
        assert media.status_code == 200 and media.headers["content-type"].startswith("video/") and len(media.content) > 1000

        assert client.post(f"/api/v1/jobs/{job_id}/compose", json={"cut_in": 2, "cut_out": 1}).status_code == 400
        assert client.post(f"/api/v1/jobs/{job_id}/compose", json={"cut_in": 0, "cut_out": 2, "template": "nope"}).status_code == 400
        body = {"cut_in": c["in_seconds"], "cut_out": c["out_seconds"], "presets": ["reels", "square"], "captions": c["captions"],
                "lower_third": {"name": "Priya Kumari", "role": "BCA 2024"}, "title": "smoke", "cut_id": c["id"]}
        r = client.post(f"/api/v1/jobs/{job_id}/compose", json=body)
        assert r.status_code == 202, r.text
        renders = r.json()["renders"]
        assert [x["preset"] for x in renders] == ["reels", "square"] and all(x["status"] == "QUEUED" for x in renders)

        for rec in renders:
            done = _wait(client, f"/api/v1/renders/{rec['id']}", lambda d: d["status"] in ("DONE", "FAILED"))
            assert done["status"] == "DONE", done.get("error")
            assert done["width"] == 1080 and done["height"] == (1920 if rec["preset"] == "reels" else 1080)
            assert Path(done["output_path"]).exists() and done["output_path"].startswith(str(composer_env["tmp"] / "renders"))
            assert done["detail"]["text_shaping"] is True and done["ffmpeg_command"].startswith(("/", "ffmpeg"))
            vid = client.get(f"/api/v1/renders/{rec['id']}/video")
            assert vid.status_code == 200 and vid.headers["content-type"] == "video/mp4" and len(vid.content) == done["size_bytes"]
            assert client.get(f"/api/v1/renders/{rec['id']}/video", headers={"Range": "bytes=0-99"}).status_code in (200, 206)
            poster = client.get(f"/api/v1/renders/{rec['id']}/poster.jpg")
            assert poster.status_code == 200 and poster.headers["content-type"] == "image/jpeg" and poster.content[:2] == b"\xff\xd8"
            srt = client.get(f"/api/v1/renders/{rec['id']}/captions.srt")
            assert srt.status_code == 200 and "-->" in srt.text
        listed = client.get(f"/api/v1/renders?job_id={job_id}").json()
        assert len(listed) == 2 and {x["id"] for x in listed} == {x["id"] for x in renders}
        assert client.delete(f"/api/v1/renders/{renders[0]['id']}").json()["deleted"] == renders[0]["id"]
        assert client.get(f"/api/v1/renders/{renders[0]['id']}").status_code == 404
        assert not Path(renders[0]["output_path"] or "/nonexistent").exists() if renders[0]["output_path"] else True


def test_template_upload_creates_a_real_template(composer_env, tiny_video: Path, tmp_path):
    from PIL import Image

    from apps.api.main import create_app

    frame = tmp_path / "frame.png"
    img = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0))
    for y in list(range(0, 415)) + list(range(1385, 1920)):
        for x in range(0, 1080, 4):
            img.putpixel((x, y), (13, 44, 94, 255))
    img.save(frame)
    with TestClient(create_app()) as client:
        assert client.post("/api/v1/composer/templates/placeholder/layers", files={"file": ("frame.png", frame.read_bytes(), "image/png")}).status_code == 400
        with frame.open("rb") as f:
            r = client.post("/api/v1/composer/templates/cimage/layers", files={"file": ("frame.png", f, "image/png")},
                            data={"preset": "reels", "layer": "frame", "x": 0, "y": 0, "replace_all": "true"})
        assert r.status_code == 201, r.text
        t = r.json()
        assert t["name"] == "cimage" and [l["name"] for l in t["layouts"]["reels"]["layers"]] == ["frame"]
        assert len(t["layouts"]["square"]["layers"]) == 2   # other presets keep the placeholder layers so they still render
        names = [x["name"] for x in client.get("/api/v1/composer/templates").json()]
        assert names == ["cimage", "placeholder"]
        assert client.get("/api/v1/composer/templates/cimage/preview?preset=reels").status_code == 200
        # edit the JSON (move the clip window) and render with it
        t["layouts"]["reels"]["video_zone"] = {"x": 0, "y": 430, "w": 1080, "h": 940}
        assert client.put("/api/v1/composer/templates/cimage", json=t).json()["layouts"]["reels"]["video_zone"]["y"] == 430
        job_id = _analysed_job(client, tiny_video)
        r = client.post(f"/api/v1/jobs/{job_id}/compose", json={"cut_in": 0, "cut_out": 2.5, "presets": ["reels"], "template": "cimage", "captions_enabled": False})
        assert r.status_code == 202, r.text
        done = _wait(client, f"/api/v1/renders/{r.json()['renders'][0]['id']}", lambda d: d["status"] in ("DONE", "FAILED"))
        assert done["status"] == "DONE" and done["template"] == "cimage", done.get("error")
        assert client.delete("/api/v1/composer/templates/cimage/layers", params={"preset": "reels", "layer": "frame"}).json()["layouts"]["reels"]["layers"] == []
