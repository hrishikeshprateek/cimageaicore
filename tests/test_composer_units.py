"""Video Composer: pure-Python units (template geometry, captions, cut selection, ffmpeg command). No ffmpeg run, no network."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.block_engine.schemas import VideoAnalysisV1
from services.video_composer.captions import CaptionCue, chunk_text, cues_for_window, to_ass, to_srt
from services.video_composer.cuts import AICutsV1, CutLimits, propose_cuts, refine_with_ai
from services.video_composer.renderer import LowerThird, RenderSpec, _Placed, build_command, font_paths
from services.video_composer.settings import ComposerSettings
from services.video_composer.framing import Subject, focus_for_center, sample_times
from services.video_composer.template import (PRESETS, Layout, Rect, Template, cover_crop, effective_fit, fit_rect, list_templates, load_template,
                                              resolve_layout)
from services.video_composer.textrender import FontPaths, has_glyph, script_runs


def analysis(**over) -> VideoAnalysisV1:
    base = {
        "video": {"title": "Student review", "video_type": "interview", "language": "hi-en", "description": "A BCA student talks about placements.", "observed_duration": "00:02:00"},
        "events": [],
        "people": [
            {"name": "Priya Kumari", "role": "BCA 2024, placed at Wipro", "context": "speaks", "timestamps": ["00:00:05"], "identified_by": "on_screen", "confidence": 0.9},
            {"name": "Speaker 2", "role": None, "context": "unnamed", "timestamps": ["00:01:30"], "identified_by": "unnamed", "confidence": 0.5},
        ],
        "transcript": [
            {"speaker": "Priya Kumari", "start_time": "00:00:00", "end_time": "00:00:40", "language": "hi-en",
             "text": "मैं CIMAGE में BCA की स्टूडेंट हूँ। यहाँ की faculty बहुत supportive है। मुझे Wipro में placement मिला। CIMAGE ने मेरी ज़िंदगी बदल दी।"},
            {"speaker": "Priya Kumari", "start_time": "00:00:40", "end_time": "00:01:20", "language": "en",
             "text": "The labs are open till late and the mentors actually sit with you. I would tell every junior to join the coding club in the first semester."},
            {"speaker": "Speaker 2", "start_time": "00:01:20", "end_time": "00:02:00", "language": "en", "text": "Thank you Priya. That was our student voice for today."},
        ],
        "topics": [],
        "key_moments": [{"timestamp": "00:00:25", "description": "Placement announcement", "importance": "high"}, {"timestamp": "00:01:25", "description": "closing", "importance": "low"}],
        "quotes": [
            {"speaker": "Priya Kumari", "text": "CIMAGE ने मेरी ज़िंदगी बदल दी।", "timestamp": "00:00:30", "source_reference": "speech at 00:00:30"},
            {"speaker": "Priya Kumari", "text": "I would tell every junior to join the coding club in the first semester.", "timestamp": "00:01:05", "source_reference": "speech"},
        ],
        "media": [],
        "summary": {"short_summary": "s", "detailed_summary": "d", "key_points": []},
        "content_opportunities": [],
    }
    base.update(over)
    return VideoAnalysisV1.model_validate(base)


# ---------------------------------------------------------------- template
def test_placeholder_template_generates_layers_for_every_preset(tmp_path: Path):
    t = load_template("placeholder", tmp_path)
    assert set(t.layouts) == set(PRESETS) and t.missing_files() == []
    for preset, (w, h) in PRESETS.items():
        lay = t.layouts[preset]
        assert (lay.width, lay.height) == (w, h)
        for layer in lay.layers:
            from PIL import Image

            img = Image.open(t.layer_path(layer))
            assert img.mode == "RGBA" and layer.x + img.width <= w and layer.y + img.height <= h
    # geometry of the real CIMAGE frame: the clip window sits between the top band (0-415) and the bottom band (1385-1920)
    reels = t.layouts["reels"]
    assert reels.video_zone.y >= 415 and reels.video_zone.bottom <= 1385
    assert [x["name"] for x in list_templates(tmp_path)] == ["placeholder"]
    # a saved template reloads identically
    again = load_template("placeholder", tmp_path)
    assert again.model_dump(mode="json") == t.model_dump(mode="json")


def test_fit_and_resolve_layout_places_captions_below_or_over_the_clip(tmp_path: Path):
    t = load_template("placeholder", tmp_path)
    lay = t.layouts["reels"]
    wide = fit_rect(1920, 1080, lay.video_zone, align=lay.video_align)
    assert wide.w == 1080 and wide.h == 608 and wide.y == lay.video_zone.y  # aligned top
    assert fit_rect(1920, 1080, lay.video_zone).y == lay.video_zone.y + (lay.video_zone.h - 608) // 2  # default centred
    tall = fit_rect(1080, 1920, lay.video_zone, align=lay.video_align)
    assert tall.h == lay.video_zone.h and tall.x > 0  # pillar-boxed
    cover = fit_rect(1080, 1920, lay.video_zone, fit="cover")
    assert (cover.w, cover.h) == (lay.video_zone.w, lay.video_zone.h)
    r_wide = resolve_layout(lay, "reels", 1920, 1080, fit="contain", caption=t.caption, caption_line_px=80, lower_third=t.lower_third)
    assert r_wide.fit == "contain" and not r_wide.captions_over_video and r_wide.caption_rect.y >= r_wide.video_rect.bottom and r_wide.caption_anchor == "top"
    r_tall = resolve_layout(lay, "reels", 1080, 1920, fit="contain", caption=t.caption, caption_line_px=80, lower_third=t.lower_third)
    assert r_tall.captions_over_video and r_tall.caption_rect.bottom <= r_tall.video_rect.bottom
    assert r_tall.lower_third_bottom <= r_tall.caption_rect.y  # lower-third sits above the captions when both are over the clip
    with pytest.raises(ValueError):
        Layout(width=1080, height=1920, video_zone=Rect(x=0, y=1000, w=1080, h=1000))
    with pytest.raises(ValueError):
        Template(name="x", layouts={"reels": Layout(width=1080, height=1080, video_zone=Rect(x=0, y=0, w=10, h=10))})


def test_auto_fit_fills_the_reel_for_wide_sources_but_letterboxes_extreme_mismatches(tmp_path: Path):
    t = load_template("placeholder", tmp_path)
    reels, landscape = t.layouts["reels"].video_zone, t.layouts["landscape"].video_zone
    assert t.layouts["reels"].fit == "auto"
    assert effective_fit("auto", 1920, 1080, reels) == "cover"       # 16:9 talk in the 9:16 window: keep 66% of the width, fill the window
    assert effective_fit("auto", 640, 360, reels) == "cover"
    assert effective_fit("auto", 1080, 1920, reels) == "cover"       # phone clip in the reel window (keeps 48%)
    assert effective_fit("auto", 1080, 1920, landscape) == "contain"  # phone clip in 16:9 would keep 32% -> letterbox instead
    assert effective_fit("auto", 1920, 1080, landscape) == "cover"   # exact match: cover == contain
    assert effective_fit("contain", 1920, 1080, reels) == "contain" and effective_fit("cover", 1080, 1920, landscape) == "cover"
    r = resolve_layout(t.layouts["reels"], "reels", 1920, 1080, fit=None, caption=t.caption, caption_line_px=80, lower_third=t.lower_third)
    assert r.fit == "cover" and (r.video_rect.w, r.video_rect.h) == (reels.w, reels.h) and r.captions_over_video


def test_cover_crop_geometry_and_focus():
    zone = Rect(x=0, y=440, w=1080, h=920)
    c = cover_crop(1920, 1080, zone)                                  # centred by default
    assert (c.scaled_w, c.scaled_h, c.w, c.h) == (1636, 920, 1080, 920) and c.y == 0 and c.x == 278
    assert cover_crop(1920, 1080, zone, focus_x=0.0).x == 0 and cover_crop(1920, 1080, zone, focus_x=1.0).x == 1636 - 1080
    assert cover_crop(1920, 1080, zone, focus_x=7.0).x == 1636 - 1080  # clamped
    tall = cover_crop(1080, 1920, zone, focus_y=0.0)                  # tall source overflows vertically instead
    assert (tall.scaled_w, tall.scaled_h, tall.x, tall.y) == (1080, 1920, 0, 0) and cover_crop(1080, 1920, zone, focus_y=1.0).y == 1000
    for v in (c.x, c.y, c.scaled_w, c.scaled_h):
        assert v % 2 == 0                                             # yuv420 needs even offsets/sizes
    # focus that centres the kept window on a subject: 66% window, subject at 0.5 -> 0.5; subject near an edge clamps to that edge
    keep = 1080 / 1636
    assert focus_for_center(0.5, keep) == 0.5 and focus_for_center(0.2, keep) == 0.0 and focus_for_center(0.9, keep) == 1.0
    assert focus_for_center(0.3, 1.0) == 0.5                          # nothing cropped -> any focus is fine
    assert sample_times(10, 22) == [11.2, 13.6, 16.0, 18.4, 20.8] and sample_times(3, 3.8) == [3.4]   # inside the cut, never at the edges
    assert Subject().method == "center" and Subject().center_x is None


# ---------------------------------------------------------------- captions
def test_chunking_and_cue_timing_from_transcript():
    a = analysis()
    chunks = chunk_text(a.transcript[0].text, 68)
    assert all(len(c) <= 68 for c in chunks) and "".join(chunks).replace(" ", "") == a.transcript[0].text.replace(" ", "")
    assert chunks[0].endswith("।")  # sentence-aware split
    cues = cues_for_window(a.transcript, 20.0, 50.0, max_chars_per_line=34, max_lines=2)
    assert cues and cues[0].start == 0 and cues[-1].end <= 30.0
    for prev, cur in zip(cues, cues[1:]):
        assert cur.start >= prev.end
    assert any("Wipro" in c.text for c in cues) and any("labs" in c.text for c in cues)
    assert all(c.speaker == "Priya Kumari" for c in cues)  # generic speakers are dropped, named ones kept
    assert cues_for_window(a.transcript, 500, 600) == []


def test_srt_and_ass_sidecars():
    cues = [CaptionCue(start=0, end=1.5, text="Hello"), CaptionCue(start=1.5, end=3.25, text="नमस्ते CIMAGE")]
    srt = to_srt(cues)
    assert "1\n00:00:00,000 --> 00:00:01,500\nHello" in srt and "00:00:03,250" in srt and "नमस्ते" in srt
    ass = to_ass(cues, width=1080, height=1920, font_size=54)
    assert "PlayResX: 1080" in ass and "Dialogue: 0,0:00:01.50,0:00:03.25,Caption" in ass and "BorderStyle" in ass
    assert to_srt([]) == ""


# ---------------------------------------------------------------- cuts
def test_rule_based_cuts_from_quotes_and_key_moments():
    a = analysis()
    cuts = propose_cuts(a, 120.0, CutLimits(min_seconds=8, max_seconds=60, target_seconds=30, max_cuts=3))
    assert 2 <= len(cuts) <= 3
    assert cuts[0].source == "quote" and cuts[0].score >= cuts[-1].score
    best = cuts[0]
    assert best.hook_line == "CIMAGE ने मेरी ज़िंदगी बदल दी।"   # quote + high key moment inside + named speaker wins
    assert best.in_seconds <= 25 <= 30 <= best.out_seconds and "key moment" in best.reason
    assert 20 <= best.in_seconds < 30   # snapped to the estimated start of the sentence before the quote, not an arbitrary lead-in
    assert best.out_seconds == 40       # runs to the end of the speaker turn
    assert 8 <= best.duration <= 60
    assert best.lower_third and best.lower_third.name == "Priya Kumari" and "Wipro" in (best.lower_third.role or "")
    assert best.captions and all(0 <= c.start < c.end <= best.duration + 0.01 for c in best.captions)
    assert best.in_ts.count(":") == 2 and best.out_ts == "00:00:40"
    second = next(c for c in cuts if c.hook_line and c.hook_line.startswith("I would tell"))
    assert second.in_seconds <= 65 <= second.out_seconds
    for i, c in enumerate(cuts):
        for other in cuts[i + 1:]:
            overlap = max(0.0, min(c.out_seconds, other.out_seconds) - max(c.in_seconds, other.in_seconds))
            assert overlap < 0.5 * min(c.duration, other.duration)
    assert all(c.out_seconds <= 120.0 for c in cuts)
    assert {c.id for c in cuts} == {c.id for c in propose_cuts(a, 120.0)}  # deterministic


def test_cuts_fall_back_to_transcript_and_respect_limits():
    a = analysis(quotes=[], key_moments=[])
    cuts = propose_cuts(a, 120.0, CutLimits(min_seconds=10, max_seconds=20, target_seconds=15, max_cuts=2))
    assert cuts and all(c.source == "transcript" and 10 <= c.duration <= 20 for c in cuts)
    empty = analysis(quotes=[], key_moments=[], transcript=[])
    fallback = propose_cuts(empty, 45.0)
    assert len(fallback) == 1 and fallback[0].in_seconds == 0 and fallback[0].captions == []
    assert propose_cuts(empty, None) == []


def test_ai_refinement_uses_gateway_and_falls_back():
    a = analysis()
    rule = propose_cuts(a, 120.0)

    class FakeProvider:
        name = "fake"

        def __init__(self, text):
            self.text = text
            self.calls = []

        def generate_structured(self, system_instruction, prompt, json_schema, *, model=None, thinking_level=None):
            from services.ai_gateway.base import RawModelOutput

            self.calls.append((system_instruction, prompt, json_schema))
            return RawModelOutput(text=self.text, model="fake")

    good = FakeProvider(json.dumps({"cuts": [{"in_time": "00:00:41", "out_time": "00:01:12", "title": "Advice to juniors", "hook_line": "join the coding club",
                                              "reason": "complete statement", "lower_third_name": "Priya Kumari", "lower_third_role": "BCA 2024"}]}))
    cuts, warning = refine_with_ai(good, a, 120.0, rule, institution_context="CIMAGE")
    assert warning is None and len(cuts) == 1 and cuts[0].source == "ai" and cuts[0].in_seconds == 41 and cuts[0].out_seconds == 72
    assert cuts[0].lower_third.name == "Priya Kumari" and cuts[0].captions
    sys_prompt, user_prompt, schema = good.calls[0]
    assert "CIMAGE" in sys_prompt and "00:00:30" in user_prompt and "rule-based" in user_prompt.lower() and schema["properties"]["cuts"]
    assert AICutsV1.model_validate_json(good.text)

    from services.ai_gateway.mock import MockProvider

    cuts, warning = refine_with_ai(MockProvider(), a, 120.0, rule)
    assert cuts == rule and "unavailable" in warning
    bad = FakeProvider("not json")
    cuts, warning = refine_with_ai(bad, a, 120.0, rule)
    assert cuts == rule and warning


# ---------------------------------------------------------------- text / fonts
def test_script_runs_and_font_fallback():
    assert script_runs("Hello दुनिया 2026!") == [("latin", "Hello "), ("deva", "दुनिया 2026!")]
    assert script_runs("") == []
    s = ComposerSettings(enabled=True)
    fp = FontPaths(regular=str(s.font_regular_path), bold=str(s.font_bold_path), fallback_regular=str(s.font_fallback_regular_path), fallback_bold=str(s.font_fallback_bold_path))
    assert has_glyph(fp.regular, "क") and has_glyph(fp.regular, "A")
    assert has_glyph(fp.fallback_regular, "क") and not has_glyph(fp.fallback_regular, "A")
    # a Latin-only primary would fall back for Devanagari runs
    latin_only = FontPaths(regular=fp.fallback_regular, bold=fp.fallback_bold, fallback_regular=fp.regular, fallback_bold=fp.bold)
    assert latin_only.path("regular", "latin") == fp.regular  # Noto Devanagari lacks Latin -> Poppins
    assert fp.measure("नमस्ते", "regular", 40) > 0


# ---------------------------------------------------------------- ffmpeg command
def test_build_command_is_deterministic_and_complete(tmp_path: Path):
    t = load_template("placeholder", tmp_path)
    s = ComposerSettings(enabled=True, templates_dir=tmp_path)
    spec = RenderSpec(job_id="j", source_path="/x/src.mp4", source_width=1920, source_height=1080, source_has_audio=True, cut_in=12.5, cut_out=40.0, preset="reels",
                      captions=[CaptionCue(start=0, end=2, text="hi")], lower_third=LowerThird(name="A", role="B"))
    fonts = font_paths(t, s)
    lay = t.layouts["reels"]
    layout = resolve_layout(lay, "reels", 1920, 1080, fit="contain", caption=t.caption, caption_line_px=fonts.line_height("regular", 54), lower_third=t.lower_third)
    layers = [_Placed(path=str(t.layer_path(l)), x=l.x, y=l.y) for l in lay.layers]
    texts = [_Placed(path="/w/cap_000.png", x=60, y=1076, start=0.0, end=2.0, kind="caption"), _Placed(path="/w/lt.png", x=36, y=900, start=0.6, end=6.0, kind="lower_third")]
    cmd = build_command(spec, t, layout, layers, texts, tmp_path / "out.mp4")
    assert cmd == build_command(spec, t, layout, layers, texts, tmp_path / "out.mp4")
    joined = " ".join(cmd)
    assert "-ss 12.500 -t 27.500 -i /x/src.mp4" in joined and joined.endswith(str(tmp_path / "out.mp4"))
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "s=1080x1920" in fc and "scale=1080:608" in fc and f"overlay=x=0:y={layout.video_rect.y}" in fc
    assert "enable='gte(t,0.000)*lt(t,2.000)'" in fc and "enable='gte(t,0.600)*lt(t,6.000)'" in fc and fc.endswith("[a]")
    assert "afade=t=out:st=27.350" in fc and "-map [a]" in joined and "-movflags +faststart" in joined and "yuv420p" in joined
    assert cmd.count("-i") == 1 + len(layers) + len(texts)
    # auto (the layout default) on a 16:9 source -> explicit scale + crop, positioned by the focus
    spec_auto = spec.model_copy(update={"fit": None, "focus_x": 1.0})
    lay_auto = resolve_layout(lay, "reels", 1920, 1080, fit=None, caption=t.caption, caption_line_px=fonts.line_height("regular", 54), lower_third=t.lower_third)
    fc_auto = build_command(spec_auto, t, lay_auto, layers, texts, tmp_path / "out.mp4")
    fc_auto = fc_auto[fc_auto.index("-filter_complex") + 1]
    assert "scale=1636:920:flags=lanczos,crop=1080:920:556:0" in fc_auto and f"overlay=x=0:y={lay.video_zone.y}" in fc_auto
    no_audio = spec.model_copy(update={"source_has_audio": False, "captions_enabled": False})
    cmd2 = build_command(no_audio, t, layout, layers, [], tmp_path / "o.mp4")
    assert "-an" in cmd2 and "[a]" not in " ".join(cmd2)
    webm = build_command(spec, t, layout, [_Placed(path="/w/frame.webm", x=0, y=0, loop_video=True)], [], tmp_path / "o.mp4")
    assert "-stream_loop -1 -c:v libvpx-vp9 -i /w/frame.webm" in " ".join(webm)
