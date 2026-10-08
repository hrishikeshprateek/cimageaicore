"""The edit as data: a timeline, and the exports that let it leave for another editor."""
from __future__ import annotations

from pathlib import Path

import pytest

from services.video_composer.captions import CaptionCue
from services.video_composer.exporters import EXPORTERS, timecode, to_edl, to_fcpxml, to_json, to_srt
from services.video_composer.timeline import Clip, FocusKey, Timeline
from services.video_composer.tracking import crop_expression, dominant_face, simplify, smooth
from services.video_composer.framing import Face


def _timeline(tmp_path: Path) -> Timeline:
    a, b = tmp_path / "talk.mp4", tmp_path / "tour.mp4"
    a.write_bytes(b"x"), b.write_bytes(b"x")
    return Timeline(title="Placement reality", preset="reels", clips=[
        Clip(job_id="j1", source_path=str(a), in_seconds=72.3, out_seconds=88.9, label="part 1",
             captions=[CaptionCue(start=0.0, end=4.0, text="हर साल TCS आती है")], note="the point"),
        Clip(job_id="j1", source_path=str(a), in_seconds=124.1, out_seconds=135.8, label="part 2",
             track=[FocusKey(t=0, x=0.3, y=0.5), FocusKey(t=5, x=0.6, y=0.5)]),
        Clip(job_id="j2", source_path=str(b), in_seconds=10.0, out_seconds=14.0, label="campus b-roll", text="CIMAGE Patna"),
    ])


def test_timeline_positions_and_bookkeeping(tmp_path: Path):
    t = _timeline(tmp_path)
    assert t.seconds == pytest.approx(16.6 + 11.7 + 4.0, abs=0.01)
    assert t.starts() == [0.0, 16.6, 28.3] and t.jobs == ["j1", "j2"]
    assert t.at(20.0).label == "part 2" and t.at(0.0).label == "part 1"
    d = t.describe()
    assert [c["at"] for c in d["clips"]] == [0.0, 16.6, 28.3] and d["clips"][1]["tracked"] is True
    cid = t.clips[2].id
    assert t.move(cid, 0).clips[0].label == "campus b-roll" and t.drop(cid).jobs == ["j1"]


def test_edl_carries_source_timecode_for_every_clip(tmp_path: Path):
    edl = to_edl(_timeline(tmp_path))
    assert "TITLE: PLACEMENT REALITY" in edl and "FCM: NON-DROP FRAME" in edl
    assert timecode(72.3) == "00:01:12:09" and timecode(0) == "00:00:00:00"
    assert "001  TALK" in edl and "00:01:12:09 00:01:28:27 00:00:00:00 00:00:16:18" in edl   # source in/out, record in/out
    assert "* FROM CLIP NAME: talk.mp4" in edl and "* FROM CLIP NAME: tour.mp4" in edl
    assert "FACE TRACKED" in edl and edl.count("\n001 ") + edl.count("\n002 ") >= 0
    assert len([l for l in edl.splitlines() if l[:3].isdigit()]) == 3                        # one event per clip


def test_fcpxml_is_well_formed_and_references_the_originals(tmp_path: Path):
    import xml.etree.ElementTree as ET

    xml = to_fcpxml(_timeline(tmp_path))
    root = ET.fromstring(xml)
    assert root.tag == "fcpxml" and root.get("version") == "1.10"
    assets = root.findall(".//asset")
    assert len(assets) == 2 and all(a.find("media-rep").get("src").startswith("file://") for a in assets)
    clips = root.findall(".//asset-clip")
    assert [c.get("offset") for c in clips] == ["0/30s", "498/30s", "849/30s"]               # frame-accurate positions
    assert clips[0].get("start") == f"{round(72.3 * 30)}/30s" and clips[0].get("duration") == f"{round(16.6 * 30)}/30s"
    assert root.find(".//sequence").get("duration") == f"{round(_timeline(tmp_path).seconds * 30)}/30s"
    assert "CIMAGE Patna" in xml and "face tracked" in xml


def test_srt_and_json_exports(tmp_path: Path):
    t = _timeline(tmp_path)
    srt = to_srt(t)
    assert srt.startswith("1\n00:00:00,000 --> 00:00:04,000\nहर साल TCS आती है")
    assert "00:00:28,300 --> 00:00:32,300" in srt and "CIMAGE Patna" in srt                  # on-screen text becomes a cue
    data = to_json(t)
    assert '"preset": "reels"' in data and '"tracked": true' in data
    assert set(EXPORTERS) == {"edl", "fcpxml", "srt", "json"}


def test_tracking_smooths_jitter_and_follows_a_real_move():
    jitter = [(i * 0.5, 0.5 + (0.01 if i % 2 else -0.01), 0.5) for i in range(10)]
    assert len(simplify(smooth(jitter))) == 2                                                 # a still speaker: no keys worth keeping
    walk = [(i * 0.5, 0.2 + i * 0.05, 0.5) for i in range(10)]
    keys = simplify(smooth(walk))
    assert len(keys) >= 2 and keys[-1][1] > keys[0][1] + 0.1                                  # the path follows the walk
    assert all(abs(b[1] - a[1]) / max(0.001, b[0] - a[0]) <= 0.26 for a, b in zip(keys, keys[1:]))   # never faster than a pan


def test_dominant_face_sticks_to_whoever_we_were_following():
    big = Face(x=0.70, y=0.3, w=0.20, h=0.25, score=0.9)
    small = Face(x=0.10, y=0.3, w=0.12, h=0.15, score=0.9)
    assert dominant_face([big, small], None) == (big.cx, big.cy)                              # nothing to follow yet: the biggest
    assert dominant_face([big, small], (0.16, 0.37)) == (small.cx, small.cy)                  # we were on the small one: stay
    assert dominant_face([], (0.5, 0.5)) is None


def test_crop_expression_is_clamped_and_piecewise():
    keys = [FocusKey(t=0, x=0.0, y=0.5), FocusKey(t=2, x=1.0, y=0.5)]
    expr = crop_expression(keys, scaled=2000, crop=1000, axis="x", duration=2)
    assert expr.startswith("'if(lt(t,2.000)") and "1000" in expr                              # ends at the right edge, not beyond
    assert crop_expression(keys, scaled=1000, crop=1000) == "0"                               # nothing to crop: no expression
    assert crop_expression([FocusKey(t=0, x=0.5, y=0.5)], scaled=2000, crop=1000) == "500"
