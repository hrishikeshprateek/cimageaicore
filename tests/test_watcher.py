"""Folder watcher: stability rules, dedupe/skip/retry behaviour, persisted index, and the end-to-end path through the API."""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest

from services.block_engine.sources import SourceError
from services.ingestion.watcher import FolderWatcher, is_candidate, scan


def test_candidate_rules(tmp_path: Path):
    ok = tmp_path / "Convocation_2026.mp4"; ok.write_bytes(b"x")
    for name in (".hidden.mp4", "._Convocation.mp4", "~$lock.mov", "copying.mp4.part", "notes.txt", "clip.MP4.tmp"):
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / ".Trash").mkdir(); (tmp_path / ".Trash" / "old.mp4").write_bytes(b"x")
    (tmp_path / "Events" / "2026").mkdir(parents=True); deep = tmp_path / "Events" / "2026" / "Fest.MOV"; deep.write_bytes(b"x")
    assert is_candidate(ok) and is_candidate(deep)
    assert not is_candidate(tmp_path / ".Trash" / "old.mp4") and not is_candidate(tmp_path / "notes.txt")
    found = scan([tmp_path, tmp_path / "does-not-exist"])
    assert set(Path(p).name for p in found) == {"Convocation_2026.mp4", "Fest.MOV"}


class FakeQueue:
    def __init__(self):
        self.submitted: list[Path] = []
        self.active = 0
        self.fail_with: Exception | None = None
        self.known: set[str] = set()

    def submit(self, path: Path) -> tuple[str, bool]:
        if self.fail_with:
            exc, self.fail_with = self.fail_with, None
            raise exc
        self.submitted.append(path)
        if path.read_bytes() in self.known:
            return "job-dup", True
        self.known.add(path.read_bytes())
        return f"job-{len(self.submitted)}", False


def make(tmp_path: Path, q: FakeQueue, **kw) -> FolderWatcher:
    root = tmp_path / "nas"; root.mkdir(exist_ok=True)
    defaults = dict(interval_seconds=1, stable_seconds=0.2, max_active_jobs=2, state_file=tmp_path / "state.json", enabled=True)
    defaults.update(kw)
    return FolderWatcher([root], submit=q.submit, active_jobs=lambda: q.active, **defaults)


def test_file_is_picked_up_only_once_it_stops_changing(tmp_path: Path):
    q = FakeQueue(); w = make(tmp_path, q)
    f = tmp_path / "nas" / "Seminar.mp4"; f.write_bytes(b"part1")
    assert w.scan_once()["queued"] == 0 and w.status().pending_stable == 1      # first sighting starts the clock
    f.write_bytes(b"part1part2")                                                # still being copied
    assert w.scan_once()["queued"] == 0 and q.submitted == []
    time.sleep(0.25)
    c = w.scan_once()
    assert c["queued"] == 1 and q.submitted == [f] and w.status().queued_total == 1 and w.status().pending_stable == 0
    assert w.scan_once()["queued"] == 0 and len(q.submitted) == 1              # unchanged: never resubmitted
    idx = w.index()[str(f)]
    assert idx["job_id"] == "job-1" and idx["action"] == "queued"
    # the same bytes under another name -> submitted, but the store says "already have it"
    g = tmp_path / "nas" / "Seminar (copy).mp4"; shutil.copy(f, g)
    w.scan_once(); time.sleep(0.25); c = w.scan_once()
    assert c["deduplicated"] == 1 and w.index()[str(g)]["job_id"] == "job-dup" and w.status().deduplicated_total == 1
    # a re-exported file (new size/mtime) is a new submission
    f.write_bytes(b"re-exported cut v2"); w.scan_once(); time.sleep(0.25); w.scan_once()
    assert len(q.submitted) == 3 and w.index()[str(f)]["job_id"] == "job-3"


def test_skip_retry_cap_pause_and_persistence(tmp_path: Path):
    q = FakeQueue(); w = make(tmp_path, q, stable_seconds=0)
    bad = tmp_path / "nas" / "corrupt.mp4"; bad.write_bytes(b"not a video")
    q.fail_with = SourceError("not a supported video file")
    w.scan_once(); c = w.scan_once()
    assert c["skipped"] == 1 and w.index()[str(bad)]["action"] == "skipped"
    assert w.scan_once()["skipped"] == 0 and len(q.submitted) == 0            # remembered: not retried every cycle
    # a transient failure (store down) is retried on a later cycle
    f = tmp_path / "nas" / "ok.mp4"; f.write_bytes(b"video-bytes")
    q.fail_with = RuntimeError("db down")
    w.scan_once(); c = w.scan_once()
    assert c["errors"] == 1 and str(f) not in w.index() and w.status().errors_total == 1
    w.scan_once(); c = w.scan_once()
    assert c["queued"] == 1 and w.index()[str(f)]["job_id"] == "job-1"
    # too many analyses in flight: wait
    g = tmp_path / "nas" / "later.mp4"; g.write_bytes(b"more-bytes"); q.active = 2
    w.scan_once(); assert w.scan_once()["queued"] == 0 and w.status().pending_stable == 1
    q.active = 0; w.set_paused(True)
    assert w.scan_once()["queued"] == 0
    w.set_paused(False)
    assert w.scan_once()["queued"] == 1
    # index survives a restart
    saved = json.loads((tmp_path / "state.json").read_text())
    assert set(saved["index"]) == {str(bad), str(f), str(g)} and saved["queued_total"] == 2
    q2 = FakeQueue(); w2 = make(tmp_path, q2, stable_seconds=0)
    assert w2.status().queued_total == 2 and w2.scan_once()["queued"] == 0 and q2.submitted == []
    assert w2.forget(str(g)) and w2.scan_once()["queued"] == 0                     # forget -> sighted again (stability clock restarts)
    assert w2.scan_once()["queued"] == 1 and q2.submitted == [g]                     # -> re-submitted on the next cycle


def test_embedding_sweep_runs_each_cycle_and_survives_failure(tmp_path: Path):
    calls = {"n": 0}

    def sweep():
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("ollama down")
        return 3 if calls["n"] == 1 else 0

    q = FakeQueue()
    w = FolderWatcher([tmp_path], submit=q.submit, active_jobs=lambda: 0, sweep_embeddings=sweep, stable_seconds=0, state_file=None)
    assert w.scan_once()["embedded"] == 3 and w.status().embedded_total == 3
    assert w.scan_once()["errors"] == 1 and w.status().recent[0]["action"] == "error"
    assert w.scan_once()["embedded"] == 0 and calls["n"] == 3


def test_watcher_thread_and_disabled_flag(tmp_path: Path):
    q = FakeQueue(); w = make(tmp_path, q, enabled=False)
    w.start(); assert w._thread is None and w.status().running is False
    w2 = make(tmp_path, q, interval_seconds=1, stable_seconds=0)
    w2.start(); assert w2.status().running
    (tmp_path / "nas" / "live.mp4").write_bytes(b"live")
    for _ in range(40):
        w2.trigger(); time.sleep(0.05)
        if q.submitted:
            break
    w2.stop()
    assert q.submitted and w2.status().running is False


def test_watcher_end_to_end_through_the_api(app_env, tiny_video: Path, monkeypatch):
    """Drop a file in the watched folder -> job -> blocks, with the mock provider and the JSON store."""
    from apps.api import config
    from fastapi.testclient import TestClient

    watch = app_env["nas"] / "AI-Test"; watch.mkdir()
    monkeypatch.setenv("WATCHER_ENABLED", "true")
    monkeypatch.setenv("WATCH_ROOTS", str(watch))
    monkeypatch.setenv("WATCHER_STABLE_SECONDS", "0")
    monkeypatch.setenv("WATCHER_INTERVAL_SECONDS", "600")     # we drive the cycles ourselves
    monkeypatch.setenv("STABLE_SECONDS", "0")
    config.get_settings.cache_clear()
    from apps.api.main import create_app

    with TestClient(create_app()) as client:
        st = client.get("/api/v1/watcher").json()
        assert st["enabled"] and st["running"] and st["roots"] == [str(watch.resolve())]
        shutil.copy(tiny_video, watch / "Seminar_Clip.mp4")
        first = client.post("/api/v1/watcher/scan?sync=true").json()
        assert first["seen"] == 1 and first["queued"] == 0                      # sighted, stability clock started
        second = client.post("/api/v1/watcher/scan?sync=true").json()
        assert second["queued"] == 1
        jobs = client.get("/api/v1/jobs").json()
        assert len(jobs) == 1 and jobs[0]["source"]["kind"] == "nas_file" and jobs[0]["source"]["name"] == "Seminar_Clip.mp4"
        for _ in range(100):
            job = client.get(f"/api/v1/jobs/{jobs[0]['id']}").json()
            if job["state"] in ("BLOCKS_COMPLETE", "FAILED"):
                break
            time.sleep(0.1)
        assert job["state"] == "BLOCKS_COMPLETE" and any(s["detail"].get("via") == "watcher" for s in job["stages"])
        shutil.copy(tiny_video, watch / "Seminar_Clip_again.mp4")               # same bytes -> deduplicated, no second job
        client.post("/api/v1/watcher/scan?sync=true"); c = client.post("/api/v1/watcher/scan?sync=true").json()
        assert c["deduplicated"] == 1 and len(client.get("/api/v1/jobs").json()) == 1
        st = client.get("/api/v1/watcher").json()
        assert st["queued_total"] == 1 and st["deduplicated_total"] == 1 and st["indexed"] == 2 and st["recent"][0]["action"] == "deduplicated"
        assert client.post("/api/v1/watcher/pause").json()["paused"] is True and client.post("/api/v1/watcher/resume").json()["paused"] is False
        idx = client.get("/api/v1/watcher/index").json()
        assert {i["action"] for i in idx} == {"queued", "deduplicated"}
        assert client.post("/api/v1/watcher/forget", json={"path": idx[0]["path"]}).json()["forgotten"] is True
        ov = client.get("/api/v1/admin/overview").json()
        assert ov["jobs"]["total"] == 1 and ov["watcher"]["queued_total"] == 1 and ov["system"]["watcher_enabled"] is True
        assert ov["blocks"]["total"] > 0 and ov["content"]["available"] is False
    assert (app_env["data"] / "watcher_state.json").exists()
