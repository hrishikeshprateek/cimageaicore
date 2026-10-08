"""The edit as an object: the AI fills a timeline, a person rearranges it, and it renders and exports."""
from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_pg_store import test_db_url  # noqa: F401 - fixture

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")


def _analysed(client, tiny_video: Path) -> str:
    with tiny_video.open("rb") as f:
        job_id = client.post("/api/v1/analyze", files={"file": (tiny_video.name, f, "video/mp4")}).json()["job_id"]
    for _ in range(200):
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["state"] in ("BLOCKS_COMPLETE", "INDEXED", "CONTENT_CANDIDATE", "FAILED"):
            break
        time.sleep(0.1)
    assert job["state"] != "FAILED"
    return job_id


def test_the_ai_fills_a_timeline_and_a_person_rearranges_it(studio_app, tiny_video: Path):
    client = studio_app
    job_id = _analysed(client, tiny_video)

    t = client.post("/api/v1/timelines", json={"job_id": job_id, "from_cuts": True, "title": "Campus"}).json()
    assert t["clips"] and t["seconds"] > 0 and t["jobs"] == [job_id]
    assert [c["at"] for c in t["clips"]][0] == 0.0                       # clips are laid end to end
    assert all(c["out_seconds"] > c["in_seconds"] for c in t["clips"])
    tid, n = t["id"], len(t["clips"])

    # a second piece of the same video - several clips cut from one video is the normal case
    t = client.post(f"/api/v1/timelines/{tid}/clips", json={"job_id": job_id, "in_seconds": 1.0, "out_seconds": 3.0, "label": "extra"}).json()
    assert len(t["clips"]) == n + 1 and t["clips"][-1]["label"] == "extra"

    # reorder, trim and mute - exactly what the studio sends back
    clips = [t["clips"][-1]] + t["clips"][:-1]
    clips[1]["out_seconds"] = clips[1]["in_seconds"] + 1.5
    clips[1]["mute"] = True
    clips[1]["text"] = "On screen"
    saved = client.put(f"/api/v1/timelines/{tid}", json={"clips": clips, "title": "Campus v2"}).json()
    assert saved["title"] == "Campus v2" and saved["clips"][0]["label"] == "extra"
    assert saved["clips"][1]["seconds"] == pytest.approx(1.5, abs=0.01) and saved["clips"][1]["mute"] is True
    assert saved["clips"][1]["at"] == saved["clips"][0]["seconds"]        # positions recomputed after the reorder

    assert client.put(f"/api/v1/timelines/{tid}", json={"clips": [{**clips[0], "out_seconds": clips[0]["in_seconds"]}]}).status_code == 400
    assert client.get("/api/v1/timelines?job_id=" + job_id).json()[0]["id"] == tid


def test_a_timeline_renders_as_one_stitched_video_and_exports(studio_app, tiny_video: Path):
    client = studio_app
    job_id = _analysed(client, tiny_video)
    t = client.post("/api/v1/timelines", json={"job_id": job_id, "from_cuts": True}).json()
    tid = t["id"]

    r = client.post(f"/api/v1/timelines/{tid}/render", json={})
    assert r.status_code == 202, r.text
    rid = r.json()["render_id"]
    for _ in range(600):
        rec = client.get(f"/api/v1/renders/{rid}").json()
        if rec["status"] in ("DONE", "FAILED"):
            break
        time.sleep(0.2)
    assert rec["status"] == "DONE", rec.get("error")
    assert rec["detail"]["timeline_id"] == tid and len(rec["detail"]["scenes"]) == len(t["clips"])
    assert rec["duration_seconds"] == pytest.approx(t["seconds"], abs=2.0)
    assert rec["width"] == 1080 and rec["height"] == 1920

    edl = client.get(f"/api/v1/timelines/{tid}/export?format=edl").text
    assert edl.count("* FROM CLIP NAME:") == len(t["clips"])
    assert "<fcpxml" in client.get(f"/api/v1/timelines/{tid}/export?format=fcpxml").text
    assert client.delete(f"/api/v1/timelines/{tid}").json()["deleted"] == tid
    assert client.get(f"/api/v1/timelines/{tid}").status_code == 404


def test_tracking_a_window_returns_a_path_or_nothing(studio_app, tiny_video: Path):
    client = studio_app
    job_id = _analysed(client, tiny_video)
    d = client.post(f"/api/v1/jobs/{job_id}/track?cut_in=0&cut_out=3").json()
    assert set(d) >= {"job_id", "keys", "detector"} and isinstance(d["keys"], list)   # a blank test clip has no faces
    assert client.post(f"/api/v1/jobs/{job_id}/track?cut_in=5&cut_out=1").status_code == 400


@pytest.fixture
def studio_app(test_db_url, tmp_path, monkeypatch):  # noqa: F811
    from apps.api import config
    from services.video_composer import settings as cs

    for k, v in {"AI_PROVIDER": "mock", "EMBEDDING_PROVIDER": "mock", "TTS_PROVIDER": "mock", "TRANSCRIBE_PROVIDER": "mock",
                 "DATABASE_URL": test_db_url, "DATA_DIR": str(tmp_path / "data"), "NAS_ALLOWED_ROOTS": str(tmp_path),
                 "GEMINI_API_KEY": "", "WATCHER_ENABLED": "false", "AUTO_DRAFT": "false", "COMPOSER_ENABLED": "true",
                 "COMPOSER_TEMPLATES_DIR": str(tmp_path / "templates"), "COMPOSER_RENDERS_DIR": str(tmp_path / "renders"),
                 "COMPOSER_X264_PRESET": "ultrafast", "COMPOSER_CRF": "32", "COMPOSER_MIN_CUT_SECONDS": "1",
                 "COMPOSER_MAX_CUT_SECONDS": "4", "COMPOSER_TARGET_CUT_SECONDS": "2"}.items():
        monkeypatch.setenv(k, v)
    config.get_settings.cache_clear()
    cs.get_composer_settings.cache_clear()
    from apps.api.main import create_app

    with TestClient(create_app()) as client:
        yield client
    config.get_settings.cache_clear()
    cs.get_composer_settings.cache_clear()
    import psycopg
    with psycopg.connect(test_db_url, autocommit=True) as conn:
        conn.execute("TRUNCATE audit_log, knowledge_blocks, processing_jobs, media, content_opportunities, drafts, draft_versions, "
                     "agent_runs, scripts, script_versions, renders, transcripts, timelines CASCADE")
