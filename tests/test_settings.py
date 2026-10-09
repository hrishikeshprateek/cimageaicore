"""The Settings page: transcription + cut lengths edited in the admin UI, applied live, inherited from .env."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from apps.api.runtime_settings import RuntimeSettings, load, save
from services.transcribe.model import Sentence, Transcript


def _fields(payload) -> dict:
    return {f["key"]: f for g in payload["groups"] for f in g["fields"]}


def test_unset_fields_inherit_env_and_saved_ones_are_marked(app_env):
    from apps.api.main import create_app

    with TestClient(create_app()) as client:
        f = _fields(client.get("/api/v1/settings").json())
        assert set(f) == {"whisper_language", "whisper_carry_context", "whisper_sentence_gap", "whisper_initial_prompt",
                          "min_cut_seconds", "max_cut_seconds", "target_cut_seconds", "max_cuts"}
        assert all(x["source"] == "env" for x in f.values())          # nothing saved yet
        assert f["whisper_carry_context"]["value"] is True            # the fix: context on by default
        assert f["whisper_sentence_gap"]["value"] == 0.6

        r = client.put("/api/v1/settings", json={"values": {"whisper_sentence_gap": 0.9, "max_cuts": 5}})
        assert r.status_code == 200
        f = _fields(r.json())
        assert f["whisper_sentence_gap"]["source"] == "ui" and f["whisper_sentence_gap"]["value"] == 0.9
        assert f["max_cuts"]["source"] == "ui" and f["max_cuts"]["value"] == 5
        assert f["min_cut_seconds"]["source"] == "env"                 # untouched fields still inherit

        # applied live: the settings objects and the rebuilt transcriber all see it
        from services.video_composer.settings import get_composer_settings

        assert client.app.state.settings.whisper_sentence_gap == 0.9
        assert get_composer_settings().max_cuts == 5
        assert getattr(client.app.state.transcriber, "sentence_gap", 0.9) == 0.9

        # only the set values are persisted, so .env keeps driving the rest
        stored = json.loads((app_env["data"] / "runtime_settings.json").read_text())
        assert stored == {"whisper_sentence_gap": 0.9, "max_cuts": 5}

        # reset hands a field back to .env
        f = _fields(client.put("/api/v1/settings", json={"values": {}, "reset": ["max_cuts"]}).json())
        assert f["max_cuts"]["source"] == "env" and f["whisper_sentence_gap"]["source"] == "ui"
        assert json.loads((app_env["data"] / "runtime_settings.json").read_text()) == {"whisper_sentence_gap": 0.9}


def test_saved_settings_survive_a_restart(app_env):
    from apps.api.main import create_app

    with TestClient(create_app()) as client:
        client.put("/api/v1/settings", json={"values": {"whisper_language": "en", "target_cut_seconds": 22}})
    with TestClient(create_app()) as client:                            # a fresh app reads the file at startup
        f = _fields(client.get("/api/v1/settings").json())
        assert f["whisper_language"]["value"] == "en" and f["whisper_language"]["source"] == "ui"
        assert f["target_cut_seconds"]["value"] == 22
        assert client.app.state.settings.whisper_language == "en"


def test_bad_values_are_refused_against_the_effective_configuration(app_env):
    from apps.api.main import create_app

    with TestClient(create_app()) as client:
        # a single value saved alone is still checked against the .env value it will combine with
        r = client.put("/api/v1/settings", json={"values": {"min_cut_seconds": 500}})
        assert r.status_code == 400 and "shorter than the longest" in r.json()["detail"]
        r = client.put("/api/v1/settings", json={"values": {"target_cut_seconds": 90}})
        assert r.status_code == 400 and "must sit between" in r.json()["detail"]
        assert client.put("/api/v1/settings", json={"values": {"whisper_language": "xx"}}).status_code == 400
        assert client.put("/api/v1/settings", json={"values": {"whisper_sentence_gap": 9}}).status_code == 400
        assert client.put("/api/v1/settings", json={"values": {"nope": 1}}).status_code == 400
        # a refused save changes nothing
        assert not (app_env["data"] / "runtime_settings.json").exists()
        assert client.put("/api/v1/settings", json={"values": {"min_cut_seconds": 10, "max_cut_seconds": 45}}).status_code == 200


def test_rerun_queues_every_video_and_the_save_is_audited(app_env, tiny_video):
    import time

    from apps.api.main import create_app

    with TestClient(create_app()) as client:
        with tiny_video.open("rb") as f:
            job_id = client.post("/api/v1/analyze", files={"file": (tiny_video.name, f, "video/mp4")}).json()["job_id"]
        deadline = time.time() + 15
        while time.time() < deadline and client.get(f"/api/v1/jobs/{job_id}").json()["state"] not in ("BLOCKS_COMPLETE", "FAILED"):
            time.sleep(0.1)
        # both actions are audited (the JSON store only logs them; Postgres keeps them for /api/v1/audit)
        seen: list[tuple[str, str]] = []
        real = client.app.state.store.audit
        client.app.state.store.audit = lambda actor, action, *a, **k: seen.append((actor, action)) or real(actor, action, *a, **k)

        r = client.post("/api/v1/transcripts/rerun")
        assert r.status_code == 202 and r.json()["queued"] >= 1
        assert client.put("/api/v1/settings", json={"values": {"max_cuts": 2}}).status_code == 200
        assert ("admin", "transcripts.rerun") in seen and ("admin", "settings.saved") in seen


def test_a_corrupt_file_is_ignored_rather_than_breaking_the_boot(tmp_path):
    p = tmp_path / "runtime_settings.json"
    p.write_text("{not json", encoding="utf-8")
    assert load(p).set_values() == {}
    save(p, RuntimeSettings(max_cuts=7))
    assert load(p).set_values() == {"max_cuts": 7}


def test_an_interrupted_measurement_is_retried_rather_than_blocking_for_ever(tmp_path):
    """Why reels kept clipping speech in production.

    `get()` only returns a transcript whose status is 'ready'. A measurement killed mid-run (a restart,
    or the connection pool closing under the background thread) left a 'running' row that `get()` would
    never return, so the cutter fell back to interpolated sentence ends - which land inside the
    speaker's last word - and never recovered. Both stores now sweep those at startup, like renders do.
    """
    from apps.api.transcript_store import JsonTranscriptStore, PostgresTranscriptStore

    assert hasattr(PostgresTranscriptStore, "mark_stale")       # the Postgres path is the one that stuck

    store = JsonTranscriptStore(tmp_path / "transcripts")
    store.mark("job1", "running")
    assert store.get("job1") is None                            # nothing for the cutter to use
    assert store.mark_stale() == 0                              # the JSON store only holds finished files

    t = Transcript(job_id="job1", seconds=4.0, sentences=[Sentence(i=0, text="CIMAGE ने मेरी ज़िंदगी बदल दी।", start=0.5, end=3.9)])
    store.put(t)
    assert store.get("job1") is not None and store.get("job1").sentences[0].end == 3.9
