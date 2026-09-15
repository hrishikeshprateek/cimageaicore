"""Upload proxies: raw/huge sources are shrunk before they reach the provider; small ones go as-is."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from services.ai_gateway.mock import MockProvider
from services.block_engine.engine import BlockEngine
from services.block_engine.proxy import GEMINI_MAX_UPLOAD_BYTES, MediaProbe, ProxyPolicy, make_proxy, probe, proxy_reason
from services.block_engine.sources import from_upload

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")


def _clip(path: Path, w=1280, h=720, seconds=3) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=25", "-f", "lavfi", "-i", "sine=frequency=440",
                    "-t", str(seconds), "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "10", "-pix_fmt", "yuv420p", str(path)], check=True, timeout=120)
    return path


def test_proxy_reason_rules():
    pol = ProxyPolicy(min_mb=400, max_bitrate_kbps=6000, max_height=720)
    small = MediaProbe(width=1280, height=720, duration=60, size_bytes=50 * 1024 ** 2, bitrate_kbps=4000, video_codec="h264", fps=25)
    assert proxy_reason(small, pol) is None                                                      # phone/H.264 export: send as-is
    assert "≥ 400 MB" in proxy_reason(MediaProbe(1920, 1080, 600, 900 * 1024 ** 2, 12000, "h264", 25), pol)
    assert "Mbps" in proxy_reason(MediaProbe(1920, 1080, 20, 100 * 1024 ** 2, 40000, "prores", 25), pol)  # short but raw
    huge = MediaProbe(3840, 2160, 240, 21 * 1024 ** 3, 700000, "prores", 25)
    assert "2 GB upload limit" in proxy_reason(huge, ProxyPolicy(enabled=False))                 # over the API limit: always
    assert proxy_reason(MediaProbe(1920, 1080, 600, 900 * 1024 ** 2, 12000, "h264", 25), ProxyPolicy(enabled=False)) is None
    assert GEMINI_MAX_UPLOAD_BYTES == 2 * 1024 ** 3


def test_make_proxy_shrinks_and_keeps_duration(tmp_path: Path):
    src = _clip(tmp_path / "raw.mp4", 1920, 1080, 3)
    info = probe(src)
    assert (info.width, info.height) == (1920, 1080) and 2.8 < info.duration < 3.3 and info.video_codec == "h264"
    out = make_proxy(src, tmp_path / "proxies" / "p.mp4", ProxyPolicy(max_height=360, crf=30, preset="ultrafast"), info=info)
    p = probe(out)
    assert (p.width, p.height) == (640, 360) and abs(p.duration - info.duration) < 0.3
    assert out.stat().st_size < src.stat().st_size / 3


class _Recording(MockProvider):
    """A non-mock-named provider that records what it was asked to upload."""
    name = "recording"

    def __init__(self):
        super().__init__(delay_seconds=0)
        self.inputs = []

    def analyze_video(self, request, on_stage):
        self.inputs.append(request.video)
        return super().analyze_video(request, on_stage)


def test_engine_uploads_a_proxy_only_when_the_policy_says_so(tmp_path: Path):
    src = from_upload(_clip(tmp_path / "Convocation_master.mp4", 1280, 720, 2))
    stages = []
    on_stage = lambda st, d: stages.append((st, d))  # noqa: E731
    # policy: proxy everything (min_mb=0) -> the provider receives the proxy, TRANSCODING stages are logged, proxy removed after
    prov = _Recording()
    eng = BlockEngine(prov, institution_context="Test", proxy=ProxyPolicy(min_mb=0, max_height=360, preset="ultrafast"), proxies_dir=tmp_path / "proxies")
    eng.analyze("job1", src, on_stage)
    sent = prov.inputs[0]
    assert sent.path != src.path and sent.path.parent == tmp_path / "proxies" and sent.mime_type == "video/mp4" and sent.name == "Convocation_master.mp4"
    tr = [d for st, d in stages if st == "TRANSCODING"]
    assert len(tr) == 2 and "reason" in tr[0] and tr[1]["done"] is True and tr[1]["ratio"] >= 1
    assert not sent.path.exists()                                       # cleaned up (PROXY_KEEP=false)
    # policy: thresholds above this clip's size/bitrate -> sent untouched, no TRANSCODING stage
    info = probe(src.path); prov2, stages2 = _Recording(), []
    BlockEngine(prov2, institution_context="Test", proxy=ProxyPolicy(min_mb=400, max_bitrate_kbps=info.bitrate_kbps + 1000), proxies_dir=tmp_path / "proxies").analyze("job2", src, lambda st, d: stages2.append(st))
    assert prov2.inputs[0].path == src.path and "TRANSCODING" not in stages2
    # keep=true leaves the proxy for reuse
    prov3 = _Recording()
    BlockEngine(prov3, institution_context="Test", proxy=ProxyPolicy(min_mb=0, max_height=360, preset="ultrafast", keep=True), proxies_dir=tmp_path / "proxies").analyze("job3", src, lambda st, d: None)
    assert prov3.inputs[0].path.exists()
    # the mock provider itself never transcodes (tests and demos stay instant)
    prov4, stages4 = MockProvider(delay_seconds=0), []
    BlockEngine(prov4, institution_context="Test", proxy=ProxyPolicy(min_mb=0), proxies_dir=tmp_path / "proxies").analyze("job4", src, lambda st, d: stages4.append(st))
    assert "TRANSCODING" not in stages4
