"""Video Composer: real ffmpeg renders of a synthetic clip (testsrc2 + tone). Offline; skipped when ffmpeg is missing."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageStat

from services.video_composer import ffmpeg as ff
from services.video_composer.captions import CaptionCue
from services.video_composer import framing
from services.video_composer.renderer import LowerThird, RenderSpec, render
from services.video_composer.settings import ComposerSettings
from services.video_composer.template import PRESETS, Layer, load_template, save_template

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg not on PATH")

CUES = [
    CaptionCue(start=0.2, end=2.5, text="CIMAGE ne meri zindagi badal di."),
    CaptionCue(start=2.5, end=5.0, text="यहाँ की faculty बहुत supportive है और placement भी शानदार हैं।"),
    CaptionCue(start=5.0, end=6.5, text="Best decision of my life."),
]


@pytest.fixture(scope="module")
def env(tmp_path_factory) -> dict:
    root = tmp_path_factory.mktemp("composer")
    settings = ComposerSettings(enabled=True, templates_dir=root / "templates", renders_dir=root / "renders", x264_preset="ultrafast", crf=28, keep_work_files=False)
    settings.ensure_dirs()
    clip = ff.make_test_clip(root / "clip.mp4", seconds=10, width=1280, height=720)
    return {"settings": settings, "template": load_template("placeholder", settings.templates_dir), "clip": clip, "info": ff.probe(clip), "root": root}


def spec_for(env, preset, **over) -> RenderSpec:
    info = env["info"]
    base = dict(job_id="t", source_path=str(env["clip"]), source_width=info.width, source_height=info.height, source_has_audio=info.has_audio,
                cut_in=1.5, cut_out=8.5, preset=preset, captions=CUES, lower_third=LowerThird(name="Priya Kumari", role="BCA 2024 · Placed at Wipro"),
                x264_preset="ultrafast", crf=28)
    base.update(over)
    return RenderSpec(**base)


def frame_at(video: Path, t: float, out: Path) -> Image.Image:
    subprocess.run([ff.FFMPEG, "-v", "error", "-y", "-ss", str(t), "-i", str(video), "-frames:v", "1", str(out)], check=True, timeout=60)
    return Image.open(out).convert("RGB")


def test_probe_synthetic_clip(env):
    info = env["info"]
    assert (info.width, info.height) == (1280, 720) and 9.5 <= info.duration <= 10.5 and info.has_audio and info.video_codec == "h264"
    assert ff.capabilities()["ffmpeg"] and "overlay" in ff.capabilities()["filters"]


@pytest.mark.parametrize("preset", list(PRESETS))
def test_render_every_preset_is_a_valid_mp4(env, preset):
    s = env["settings"]
    out = render(spec_for(env, preset), env["template"], s, s.renders_dir / "t" / f"r_{preset}.mp4")
    info = ff.probe(Path(out.output_path))
    assert (info.width, info.height) == PRESETS[preset]
    assert abs(info.duration - 7.0) < 0.25 and info.has_audio and info.video_codec == "h264"
    assert out.size_bytes > 50_000 and Path(out.captions_srt).exists() and Path(out.captions_ass).exists()
    assert out.poster_path and Path(out.poster_path).stat().st_size > 5_000 and Image.open(out.poster_path).width <= 720
    assert "नमस्ते" not in Path(out.captions_srt).read_text() and "supportive" in Path(out.captions_srt).read_text(encoding="utf-8")
    assert not (s.renders_dir / "t" / f"r_{preset}_work").exists()   # work dir cleaned up
    assert out.text_shaping is True   # Devanagari was shaped (in-process raqm or the FriBiDi child process)


def test_captions_and_lower_third_are_visible_in_the_frame(env, tmp_path):
    s = env["settings"]
    with_text = render(spec_for(env, "reels"), env["template"], s, s.renders_dir / "t" / "cap_on.mp4")
    without = render(spec_for(env, "reels", captions_enabled=False, lower_third=None), env["template"], s, s.renders_dir / "t" / "cap_off.mp4")
    lay = with_text.layout
    a = frame_at(Path(with_text.output_path), 3.5, tmp_path / "a.png")   # inside cue 2 and the lower-third window
    b = frame_at(Path(without.output_path), 3.5, tmp_path / "b.png")
    cap = lay.caption_rect
    diff_cap = ImageStat.Stat(ImageChops.difference(a.crop((cap.x, cap.y, cap.right, cap.bottom)), b.crop((cap.x, cap.y, cap.right, cap.bottom)))).mean
    assert sum(diff_cap) / 3 > 3.0, "caption area must differ between captioned and caption-less renders"
    v = lay.video_rect
    lt_box = (lay.lower_third_x, lay.lower_third_bottom - 120, lay.lower_third_x + 400, lay.lower_third_bottom)
    diff_lt = ImageStat.Stat(ImageChops.difference(a.crop(lt_box), b.crop(lt_box))).mean
    assert sum(diff_lt) / 3 > 3.0, "lower-third must be visible at 3.5s"
    # after the lower-third window (6s) and after the last cue the two renders match inside the clip
    a2 = frame_at(Path(with_text.output_path), 6.8, tmp_path / "a2.png")
    b2 = frame_at(Path(without.output_path), 6.8, tmp_path / "b2.png")
    diff_late = ImageStat.Stat(ImageChops.difference(a2.crop(lt_box), b2.crop(lt_box))).mean
    assert sum(diff_late) / 3 < 2.0
    # template band is on the canvas: top-left pixel is the brand navy, not black
    r, g, bl = a.getpixel((10, 10))
    assert bl > r and bl > 40


def test_alpha_video_layer_and_silent_source(env, tmp_path):
    s = env["settings"]
    if "prores_ks" not in ff.capabilities()["encoders"]:
        pytest.skip("no prores_ks encoder")
    # a 2-second ProRes 4444 layer with a translucent moving box, looped over a 7 s cut
    layer = env["template"].dir / "reels_motion.mov"
    subprocess.run([ff.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0xff0000@0.5:s=300x120:r=30,format=yuva444p10le", "-t", "2",
                    "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", str(layer)], check=True, timeout=120)
    t = env["template"].model_copy(deep=True)
    t.name = "motion"
    t.dir = env["template"].dir
    t.layouts["reels"].layers.append(Layer(name="motion", file=layer.name, x=700, y=1400, z=5))
    save_template(t, s.templates_dir)
    shutil.copy(layer, s.templates_dir / "motion" / layer.name)
    for l in t.layouts["reels"].layers:
        if l.file != layer.name:
            shutil.copy(env["template"].dir / l.file, s.templates_dir / "motion" / l.file)
    t = load_template("motion", s.templates_dir)
    silent = ff.make_test_clip(tmp_path / "silent.mp4", seconds=10, audio=False)
    info = ff.probe(silent)
    assert not info.has_audio
    spec = spec_for(env, "reels", source_path=str(silent), source_has_audio=False)
    out = render(spec, t, s, s.renders_dir / "t" / "motion.mp4")
    got = ff.probe(Path(out.output_path))
    assert (got.width, got.height) == (1080, 1920) and not got.has_audio and abs(got.duration - 7.0) < 0.25
    assert "-stream_loop" in out.ffmpeg_command and "-an" in out.ffmpeg_command
    frame = frame_at(Path(out.output_path), 5.0, tmp_path / "m.png")
    r, g, b = frame.getpixel((850, 1460))
    assert r > 120 and r > g + 40   # the red translucent layer is composited over the bottom band


def test_cover_crop_follows_focus(env, tmp_path):
    """16:9 testsrc2 in the 9:16 reel: auto -> fill the window; focus 0 and 1 show different parts of the source (left vs right bars)."""
    s = env["settings"]
    frames = {}
    for fx in (0.0, 1.0):
        out = render(spec_for(env, "reels", captions=[], lower_third=None, focus_x=fx), env["template"], s, s.renders_dir / "t" / f"focus_{fx}.mp4")
        frames[fx] = frame_at(Path(out.output_path), 2.0, tmp_path / f"focus_{fx}.png").convert("RGB")
    zone = env["template"].layouts["reels"].video_zone
    a, b = (f.crop((0, zone.y + 10, zone.w, zone.bottom - 10)) for f in (frames[0.0], frames[1.0]))
    assert a.size == b.size and a.size[0] == 1080
    diff = ImageStat.Stat(ImageChops.difference(a, b)).mean
    assert sum(diff) / 3 > 20            # a different slice of the source is visible
    # nothing letterboxed: no full-width band of the template's background colour inside the clip window
    bg = frames[0.0].getpixel((5, zone.y - 5))
    rows = [frames[0.0].getpixel((540, y)) for y in range(zone.y + 5, zone.bottom - 5, 25)]
    assert not any(all(abs(px[i] - bg[i]) < 8 for i in range(3)) for px in rows)
    letter = render(spec_for(env, "reels", captions=[], lower_third=None, fit="contain"), env["template"], s, s.renders_dir / "t" / "letter.mp4")
    lf = frame_at(Path(letter.output_path), 2.0, tmp_path / "letter.png").convert("RGB")
    assert all(abs(lf.getpixel((540, zone.bottom - 20))[i] - bg[i]) < 8 for i in range(3))   # contain leaves background below the 608px strip


def test_locate_subject_never_raises_and_reports_sampling(env):
    """testsrc2 has no faces: the answer is 'center' with the frames it looked at; without OpenCV it is 'center' with none."""
    sub = framing.locate_subject(env["clip"], 1.0, 6.0)
    assert sub.method == "center" and sub.center_x is None
    assert sub.frames_sampled == (5 if framing.detector_available() else 0)
    assert framing.locate_subject(env["root"] / "missing.mp4", 0, 3).method == "center"   # unreadable source -> still an answer


def test_render_rejects_bad_specs(env, tmp_path):
    s = env["settings"]
    with pytest.raises(ValueError):
        render(spec_for(env, "reels", cut_in=5, cut_out=5), env["template"], s, tmp_path / "x.mp4")
    broken = env["template"].model_copy(deep=True)
    broken.name = "broken"
    broken.layouts["reels"].layers.append(Layer(name="missing", file="nope.png"))
    save_template(broken, s.templates_dir)
    with pytest.raises(FileNotFoundError):
        render(spec_for(env, "reels"), load_template("broken", s.templates_dir), s, tmp_path / "y.mp4")
