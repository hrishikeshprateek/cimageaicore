"""Script -> voiceover -> finished video: the storyboard planner, the mock voice and a real ffmpeg render. Offline."""
from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.script_store import Script
from services.tts import MockTTS, spoken_seconds
from services.video_composer.storyboard import plan, ts_seconds
from tests.test_content import pg_app  # noqa: F401 - fixture
from tests.test_pg_store import test_db_url  # noqa: F401 - fixture

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")


class _Job:
    def __init__(self, path, duration=30.0, name="Open day"):
        self.id = "job1"
        self.media_id = "m1"
        self.source = type("S", (), {"path": str(path) if path else None, "duration_seconds": duration, "name": name})()


def _script(scenes, evidence_blocks=(), **kw):
    now = "2026-09-27T10:00:00+00:00"
    return Script(id="s1", status="new", idea="a reel", scenes=scenes, evidence={"blocks": list(evidence_blocks)},
                  created_at=now, updated_at=now, **kw)


def test_timestamps_and_the_three_kinds_of_scene(tmp_path: Path, tiny_video: Path):
    assert ts_seconds("00:01:06") == 66 and ts_seconds("1:06") == 66 and ts_seconds("12.5") == 12.5 and ts_seconds(None) is None
    still = tmp_path / "shot.jpg"
    still.write_bytes(b"x")
    sc = _script(
        [{"n": 1, "seconds": 6, "b_roll_block_id": "job1:quote:0", "on_screen_text": "Placements"},
         {"n": 2, "seconds": 5, "b_roll_image_id": "img1"},
         {"n": 3, "seconds": 4, "visual": "wide shot of the new lab"},
         {"n": 4, "seconds": 4, "b_roll_block_id": "gone:quote:0", "b_roll_image_id": "img1"},
         {"n": 5, "seconds": 4, "b_roll_block_id": "yt:quote:0", "b_roll_image_id": "img1"}],   # analysed from a YouTube link: no file
        [{"block_id": "job1:quote:0", "timestamp": "00:00:20"}, {"block_id": "yt:quote:0", "timestamp": "00:00:05"}],
    )
    board = plan(sc, job_for=lambda jid: _Job(tiny_video) if jid == "job1" else (_Job(None) if jid == "yt" else None),
                 image_path=lambda i: still if i == "img1" else None)
    kinds = [s.kind for s in board.segments]
    assert kinds == ["clip", "still", "slate", "still", "still"]              # a video we do not have falls back to the picture
    assert all(s.kind != "clip" or s.source_path for s in board.segments)
    assert board.segments[0].cut_in == 20 and board.segments[0].text == "Placements"
    assert board.job_id == "job1" and board.seconds == 23
    assert any("no footage yet" in w for w in board.warnings) and any("not on this machine" in w for w in board.warnings)


def test_a_clip_never_runs_past_the_end_of_its_source(tiny_video: Path):
    sc = _script([{"n": 1, "seconds": 8, "b_roll_block_id": "job1:quote:0"}], [{"block_id": "job1:quote:0", "timestamp": "00:00:28"}])
    board = plan(sc, job_for=lambda jid: _Job(tiny_video, duration=30.0), image_path=lambda i: None)
    assert board.segments[0].cut_in == 22.0                                    # 30s source, 8s scene -> starts at 22, not 28


def test_mock_voice_writes_audio_of_about_the_right_length(tmp_path: Path):
    out = MockTTS().speak("यह एक छोटी लाइन है जो लगभग तीन सेकंड लेती है", tmp_path / "v.mp3")
    assert out.exists() and out.stat().st_size > 200
    assert 3 < spoken_seconds("one two three four five six seven eight nine ten", "en") < 5


def test_voiceover_and_video_through_the_api(pg_app_video, tiny_video: Path):  # noqa: F811
    client, tmp = pg_app_video
    with tiny_video.open("rb") as f:
        job_id = client.post("/api/v1/analyze", files={"file": (tiny_video.name, f, "video/mp4")}).json()["job_id"]
    for _ in range(150):
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["state"] in ("CONTENT_CANDIDATE", "INDEXED", "FAILED"):
            break
        time.sleep(0.1)
    assert job["state"] != "FAILED"

    opts = client.get("/api/v1/script-options").json()
    assert opts["voiceover"]["provider"] == "mock" and opts["composer"] is True

    sid = client.post("/api/v1/scripts", json={"idea": "campus ka chhota reel", "language": "hi", "seconds": 20, "job_id": job_id}).json()["id"]
    for _ in range(150):
        s = client.get(f"/api/v1/scripts/{sid}").json()
        if s["status"] != "generating":
            break
        time.sleep(0.1)
    assert s["status"] == "new", s.get("error")

    voices = client.get("/api/v1/tts/voices").json()
    assert voices["provider"] == "mock" and voices["voices"][0]["id"] == "mock-voice"

    # a video needs a voiceover (or the footage's own sound)
    assert client.post(f"/api/v1/scripts/{sid}/video", json={}).status_code == 400

    v = client.post(f"/api/v1/scripts/{sid}/voiceover", json={"voice_id": "mock-voice"}).json()
    vo = v["extras"]["voiceover"]
    assert vo["provider"] == "mock" and len(vo["scenes"]) == len(s["scenes"]) and vo["seconds"] > 0
    assert all(Path(x["path"]).exists() for x in vo["scenes"])
    assert v["version"] == s["version"] + 1                                    # generating a voiceover is a versioned edit
    assert all(sc["seconds"] >= next(x["seconds"] for x in vo["scenes"] if x["n"] == sc["n"]) for sc in v["scenes"])   # scenes fit their lines
    assert client.get(f"/api/v1/scripts/{sid}/audio/1").headers["content-type"] == "audio/mpeg"

    # a voice id the key does not have (a page left open through a config change) falls back instead of failing
    bad = client.post(f"/api/v1/scripts/{sid}/voiceover", json={"voice_id": "gone-voice", "scenes": [1]}).json()
    vo1 = bad["extras"]["voiceover"]
    assert vo1["voice_id"] == "mock-voice" and any("not a voice on this key" in w for w in vo1["warnings"])
    assert client.get(f"/api/v1/scripts/{sid}/audio/99").status_code == 404

    r = client.post(f"/api/v1/scripts/{sid}/video", json={"preset": "reels", "audio": "voiceover"})
    assert r.status_code == 202, r.text
    rid = r.json()["render_id"]
    assert r.json()["scenes"] == len(v["scenes"])
    for _ in range(600):
        rec = client.get(f"/api/v1/renders/{rid}").json()
        if rec["status"] in ("DONE", "FAILED"):
            break
        time.sleep(0.2)
    assert rec["status"] == "DONE", rec.get("error")
    assert rec["width"] == 1080 and rec["height"] == 1920                       # the reel preset, from the template
    assert abs(rec["duration_seconds"] - sum(x["seconds"] for x in v["scenes"])) < 2.5
    assert rec["detail"]["kind"] == "storyboard" and len(rec["detail"]["scenes"]) == len(v["scenes"]) and rec["detail"]["voice"] is True
    assert client.get(f"/api/v1/renders/{rid}/video").headers["content-type"] == "video/mp4"

    listed = client.get(f"/api/v1/scripts/{sid}/video").json()
    assert [x["id"] for x in listed] == [rid] and listed[0]["url"].endswith("/video")


@pytest.fixture
def pg_app_video(test_db_url, tmp_path, monkeypatch):  # noqa: F811
    """The API with Postgres, the mock AI, the mock voice and the composer switched on."""
    from apps.api import config
    from services.video_composer import settings as cs

    monkeypatch.setenv("AI_PROVIDER", "mock")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "mock")
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NAS_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("WATCHER_ENABLED", "false")
    monkeypatch.setenv("AUTO_DRAFT", "false")
    monkeypatch.setenv("TTS_PROVIDER", "mock")
    monkeypatch.setenv("COMPOSER_ENABLED", "true")
    monkeypatch.setenv("COMPOSER_TEMPLATES_DIR", str(tmp_path / "templates"))
    monkeypatch.setenv("COMPOSER_RENDERS_DIR", str(tmp_path / "renders"))
    monkeypatch.setenv("COMPOSER_X264_PRESET", "ultrafast")
    monkeypatch.setenv("COMPOSER_CRF", "32")
    config.get_settings.cache_clear()
    cs.get_composer_settings.cache_clear()
    from apps.api.main import create_app

    with TestClient(create_app()) as client:
        yield client, tmp_path
    config.get_settings.cache_clear()
    cs.get_composer_settings.cache_clear()
    import psycopg
    with psycopg.connect(test_db_url, autocommit=True) as conn:
        conn.execute("TRUNCATE audit_log, knowledge_blocks, processing_jobs, media, content_opportunities, drafts, draft_versions, agent_runs, scripts, script_versions, renders CASCADE")
