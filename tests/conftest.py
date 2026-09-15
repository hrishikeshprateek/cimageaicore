import os
import subprocess
import shutil
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def tiny_video(tmp_path_factory) -> Path:
    """A 3-second real MP4 made with ffmpeg (falls back to a fake file if ffmpeg is missing)."""
    p = tmp_path_factory.mktemp("media") / "Test_Seminar_Clip.mp4"
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        subprocess.run(
            [ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10", "-f", "lavfi",
             "-i", "sine=frequency=440", "-t", "3", "-shortest", "-pix_fmt", "yuv420p", str(p)],
            check=True,
        )
    else:
        p.write_bytes(b"\x00" * 2048)
    return p


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    """Isolated data dir + mock provider for API tests."""
    data = tmp_path / "data"
    nas = tmp_path / "nas"
    nas.mkdir()
    monkeypatch.setenv("AI_PROVIDER", "mock")
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("NAS_ALLOWED_ROOTS", str(nas))
    monkeypatch.setenv("STABLE_SECONDS", "0.2")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "mock")   # never the developer's .env choice (ollama/gemini) in tests
    monkeypatch.setenv("EMBEDDING_MODEL", "auto")
    monkeypatch.setenv("WATCHER_ENABLED", "false")      # tests drive the watcher explicitly
    monkeypatch.setenv("AUTO_DRAFT", "false")
    monkeypatch.setenv("DATABASE_URL", "")
    from apps.api import config

    config.get_settings.cache_clear()
    yield {"data": data, "nas": nas}
    config.get_settings.cache_clear()
