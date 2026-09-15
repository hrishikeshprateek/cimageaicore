"""Still-image preview of a template layout (no ffmpeg): layers over a stand-in clip, sample caption and lower-third."""
from __future__ import annotations

import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

from services.video_composer.renderer import font_paths
from services.video_composer.settings import ComposerSettings
from services.video_composer.template import Template, resolve_layout
from services.video_composer.textrender import CaptionJob, LowerThirdJob, _hex_rgba, render_jobs

SAMPLE_CAPTION = "यहाँ की faculty बहुत supportive है — placements भी शानदार हैं।"


def template_preview(template: Template, preset: str, settings: ComposerSettings, *, source_aspect: tuple[int, int] = (16, 9),
                     caption: str = SAMPLE_CAPTION, name: str = "Student Name", role: str | None = "BCA 2024 · Placed at Wipro") -> Image.Image:
    lay = template.layouts[preset]
    fonts = font_paths(template, settings)
    cap_style = template.caption
    font_size = lay.caption_font_size or cap_style.font_size
    cap_line = fonts.line_height("bold" if cap_style.bold else "regular", font_size, cap_style.line_height)
    src_w, src_h = source_aspect[0] * 120, source_aspect[1] * 120
    layout = resolve_layout(lay, preset, src_w, src_h, fit=None, caption=cap_style, caption_line_px=cap_line, lower_third=template.lower_third)

    img = Image.new("RGBA", (lay.width, lay.height), _hex_rgba(lay.background))
    d = ImageDraw.Draw(img)
    v = layout.video_rect
    d.rectangle((v.x, v.y, v.right - 1, v.bottom - 1), fill=(70, 74, 82, 255))
    label = f"review clip {source_aspect[0]}:{source_aspect[1]}  ·  {v.w}x{v.h}  ·  {layout.fit}"
    w = fonts.measure(label, "regular", 28)
    fonts.draw(d, (v.x + (v.w - w) / 2, v.y + v.h / 2 - 20), label, "regular", 28, (200, 204, 210, 255))
    for layer in sorted((l for l in lay.layers if l.enabled), key=lambda l: l.z):
        p = template.layer_path(layer)
        if p.suffix.lower() == ".png" and p.exists():
            li = Image.open(p).convert("RGBA")
            img.alpha_composite(li, (layer.x, layer.y))
        else:   # video layer or missing file: show its footprint
            d.rectangle((layer.x, layer.y, min(lay.width, layer.x + 400) - 1, min(lay.height, layer.y + 60) - 1), outline=(245, 197, 66, 255), width=3)
            fonts.draw(d, (layer.x + 12, layer.y + 12), f"{layer.name}: {layer.file}", "regular", 24, (245, 197, 66, 255))
    with tempfile.TemporaryDirectory() as tmp:
        jobs = [CaptionJob(text=caption, width=layout.caption_rect.w, style=cap_style, font_size=layout.caption_font_size, fonts=fonts, out=f"{tmp}/cap.png")]
        if name:
            jobs.append(LowerThirdJob(name=name, role=role, style=template.lower_third, scale=layout.lower_third_scale, fonts=fonts, out=f"{tmp}/lt.png"))
        results, _ = render_jobs(jobs, fribidi_override=settings.fribidi_lib_dir)
        cap = Image.open(results[0].out).convert("RGBA")
        y = layout.caption_rect.y if layout.caption_anchor == "top" else layout.caption_rect.bottom - cap.height
        img.alpha_composite(cap, (layout.caption_rect.x, max(0, y)))
        if len(results) > 1:
            lt = Image.open(results[1].out).convert("RGBA")
            img.alpha_composite(lt, (layout.lower_third_x, max(0, layout.lower_third_bottom - lt.height)))
    if lay.safe_area:
        s = lay.safe_area
        d.rectangle((s.x, s.y, s.right - 1, s.bottom - 1), outline=(255, 255, 255, 90), width=2)
    return img
