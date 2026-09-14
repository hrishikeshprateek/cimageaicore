import shutil
import time
from pathlib import Path

from fastapi.testclient import TestClient


def _wait(client, job_id, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["state"] in ("BLOCKS_COMPLETE", "FAILED"):
            return job
        time.sleep(0.1)
    raise AssertionError("job did not finish")


def test_upload_to_blocks(app_env, tiny_video: Path):
    from apps.api.main import create_app

    with TestClient(create_app()) as client:
        assert client.get("/api/v1/system").json()["provider"] == "mock"
        with tiny_video.open("rb") as f:
            r = client.post("/api/v1/analyze", files={"file": (tiny_video.name, f, "video/mp4")})
        assert r.status_code == 202, r.text
        job_id = r.json()["job_id"]
        job = _wait(client, job_id)
        assert job["state"] == "BLOCKS_COMPLETE", job.get("error")
        assert [s["state"] for s in job["stages"]] == ["RECEIVED", "STABLE", "QUEUED", "UPLOADED", "ANALYZING", "ANALYZING", "BLOCKS_PARTIAL", "BLOCKS_COMPLETE"]
        blocks = client.get(f"/api/v1/jobs/{job_id}/blocks").json()
        assert len(blocks) == sum(job["block_counts"].values()) + 2  # + video + summary
        assert client.get(f"/api/v1/jobs/{job_id}/blocks?block_type=quote").json()[0]["block_type"] == "quote"
        assert client.get(f"/api/v1/jobs/{job_id}/result").json()["analysis"]["video"]["title"].startswith("[MOCK]")
        # persisted to disk
        assert (app_env["data"] / "analyses" / f"{job_id}.json").exists()


def test_nas_path_allowlist_and_stability(app_env, tiny_video: Path):
    from apps.api.main import create_app

    inside = app_env["nas"] / "clip.mp4"
    shutil.copy(tiny_video, inside)
    with TestClient(create_app()) as client:
        r = client.post("/api/v1/analyze", data={"path": str(tiny_video)})  # outside allowed roots
        assert r.status_code == 400 and "allowed roots" in r.json()["detail"]
        r = client.post("/api/v1/analyze", data={"path": str(inside)})
        assert r.status_code == 202
        job = _wait(client, r.json()["job_id"])
        assert job["state"] == "BLOCKS_COMPLETE" and job["source"]["kind"] == "nas_file"


def test_online_source_validation(app_env):
    from apps.api.main import create_app

    with TestClient(create_app()) as client:
        assert client.post("/api/v1/analyze", data={"url": "https://example.com/video.mp4"}).status_code == 400
        r = client.post("/api/v1/analyze", data={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"})
        assert r.status_code == 202 and r.json()["source"]["kind"] == "online"
        assert client.post("/api/v1/analyze", data={}).status_code == 400
        assert client.get("/api/v1/jobs/nope").status_code == 404


def test_duplicate_upload_returns_existing_job(app_env, tiny_video: Path):
    from apps.api.main import create_app

    with TestClient(create_app()) as client:
        with tiny_video.open("rb") as f:
            first = client.post("/api/v1/analyze", files={"file": (tiny_video.name, f, "video/mp4")}).json()
        _wait(client, first["job_id"])
        with tiny_video.open("rb") as f:
            second = client.post("/api/v1/analyze", files={"file": ("renamed.mp4", f, "video/mp4")}).json()
        assert second["deduplicated"] is True and second["job_id"] == first["job_id"]
        assert len(client.get("/api/v1/jobs").json()) == 1
        hits = client.get("/api/v1/search", params={"q": "placeholder"}).json()
        assert hits and hits[0]["job_id"] == first["job_id"]
