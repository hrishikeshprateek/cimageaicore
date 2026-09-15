"""Template model: canvas per export preset, video zone, branded layers, text styles.

A template is a directory:  <templates_dir>/<name>/template.json  + the layer files it references
(PNG with alpha, .mov ProRes 4444 or .webm VP9-alpha). The `placeholder` template is generated
with Pillow so nothing waits on the real college assets; when they arrive they slot into a new
directory (or are uploaded through the API) and `COMPOSER_TEMPLATE=<name>` selects them.

Geometry of the real CIMAGE 9:16 frame (top band 0-415, clip window 415-1385, bottom band 1385-1920)
is what the placeholder `reels` layout mirrors, so the real PNG drops in unchanged.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

Preset = Literal["reels", "square", "landscape"]
Fit = Literal["contain", "cover", "auto"]
# "auto" fills the clip window (cover) unless the crop would throw away too much of the source -
# a 16:9 talk in the 9:16 reel keeps 66% and gets cropped; a 9:16 phone clip in the 16:9 preset would keep 32% and is letterboxed instead.
COVER_MIN_RETAINED = 0.45
PRESETS: dict[str, tuple[int, int]] = {"reels": (1080, 1920), "square": (1080, 1080), "landscape": (1920, 1080)}
PRESET_LABELS = {"reels": "Reels / Shorts 9:16", "square": "Feed 1:1", "landscape": "YouTube 16:9"}
LAYER_EXTENSIONS = {".png", ".mov", ".webm"}


class Rect(BaseModel):
    x: int
    y: int
    w: int
    h: int

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    def inset(self, dx: int, dy: int | None = None) -> "Rect":
        dy = dx if dy is None else dy
        return Rect(x=self.x + dx, y=self.y + dy, w=max(0, self.w - 2 * dx), h=max(0, self.h - 2 * dy))


class Layer(BaseModel):
    name: str
    file: str = Field(description="Relative to the template directory. .png (alpha) / .mov (ProRes 4444) / .webm (VP9 alpha).")
    x: int = 0
    y: int = 0
    z: int = 0                      # draw order among layers (captions and lower-third are drawn after all layers)
    loop: bool = True               # video layers: loop for the length of the cut
    enabled: bool = True


class CaptionStyle(BaseModel):
    font_size: int = 54
    line_height: float = 1.0        # multiplier on the font's natural (ascent+descent) line height
    colour: str = "#ffffff"
    box_colour: str = "#000000"
    box_opacity: float = Field(0.6, ge=0, le=1)
    box_radius: int = 18
    padding: int = 22
    stroke_colour: str | None = None
    stroke_width: int = 0
    max_lines: int = 2
    align: Literal["center", "left"] = "center"
    bold: bool = False


class LowerThirdStyle(BaseModel):
    name_size: int = 42
    role_size: int = 28
    colour: str = "#ffffff"
    bg_colour: str = "#0d2c5e"
    accent_colour: str = "#f5c542"
    bg_opacity: float = Field(0.92, ge=0, le=1)
    radius: int = 12
    padding: int = 18
    accent_width: int = 10
    max_width: int = 760
    start: float = 0.6              # seconds into the cut
    end: float = 6.0


class Brand(BaseModel):
    name: str = "CIMAGE Group of Institutions"
    short_name: str = "CIMAGE"
    tagline: str = "Knowledge . Skill . Success"
    contact: str = "www.cimage.in"
    primary: str = "#0d2c5e"        # navy
    accent: str = "#f5c542"         # gold
    text: str = "#ffffff"


class Layout(BaseModel):
    width: int
    height: int
    video_zone: Rect                                   # the source clip is fitted inside this rectangle
    video_align: Literal["center", "top", "bottom"] = "center"
    fit: Fit = "auto"                                  # contain = letterbox, cover = fill + crop, auto = decide from the source aspect
    background: str = "#000000"
    layers: list[Layer] = Field(default_factory=list)
    caption_zone: Rect | None = None                   # None = below the clip when there is room, else over its bottom edge
    caption_anchor: Literal["top", "bottom"] = "bottom"
    caption_gap: int = 28
    caption_margin: int = 48                           # left/right inset of captions inside their area
    caption_font_size: int | None = None               # overrides Template.caption.font_size
    lower_third_margin: int = 36
    lower_third_scale: float = 1.0
    safe_area: Rect | None = None                      # platform UI (reels: right rail, bottom bar) - keep text out of it

    @model_validator(mode="after")
    def _check(self) -> "Layout":
        z = self.video_zone
        if z.x < 0 or z.y < 0 or z.right > self.width or z.bottom > self.height:
            raise ValueError("video_zone must lie inside the canvas")
        return self


class Template(BaseModel):
    name: str
    version: int = 1
    description: str = ""
    brand: Brand = Field(default_factory=Brand)
    caption: CaptionStyle = Field(default_factory=CaptionStyle)
    lower_third: LowerThirdStyle = Field(default_factory=LowerThirdStyle)
    font_regular: str | None = None                    # relative to the template dir; blank = COMPOSER_FONT_REGULAR / bundled
    font_bold: str | None = None
    layouts: dict[str, Layout]
    dir: Path | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _check(self) -> "Template":
        for preset, lay in self.layouts.items():
            if preset not in PRESETS:
                raise ValueError(f"unknown preset '{preset}' (known: {', '.join(PRESETS)})")
            if (lay.width, lay.height) != PRESETS[preset]:
                raise ValueError(f"layout '{preset}' must be {PRESETS[preset][0]}x{PRESETS[preset][1]}")
        return self

    def layer_path(self, layer: Layer) -> Path:
        if self.dir is None:
            raise ValueError("template has no directory")
        return (self.dir / layer.file).resolve()

    def font_path(self, which: Literal["regular", "bold"], default: Path) -> Path:
        rel = self.font_regular if which == "regular" else self.font_bold
        if rel and self.dir is not None and (self.dir / rel).exists():
            return self.dir / rel
        return default

    def missing_files(self, preset: str | None = None) -> list[str]:
        return [
            f"{p}:{layer.name} -> {layer.file}"
            for p, lay in self.layouts.items()
            if preset is None or p == preset
            for layer in lay.layers
            if layer.enabled and (self.dir is None or not (self.dir / layer.file).exists())
        ]


# --------------------------------------------------------------------------------------------
# loading / saving
# --------------------------------------------------------------------------------------------

def template_path(templates_dir: Path, name: str) -> Path:
    if not name or any(ch in name for ch in "/\\.") or name.startswith("_"):
        raise ValueError(f"invalid template name '{name}'")
    return templates_dir / name / "template.json"


def save_template(t: Template, templates_dir: Path) -> Path:
    p = template_path(templates_dir, t.name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(t.model_dump(mode="json"), indent=2, ensure_ascii=False), encoding="utf-8")
    t.dir = p.parent
    return p


def load_template(name: str, templates_dir: Path, *, fonts=None) -> Template:
    """Load <templates_dir>/<name>/template.json; the placeholder is (re)generated when missing."""
    p = template_path(templates_dir, name)
    if not p.exists():
        if name == "placeholder":
            from services.video_composer.placeholder import generate_placeholder

            return generate_placeholder(templates_dir, fonts=fonts)
        raise FileNotFoundError(f"template '{name}' not found ({p})")
    t = Template.model_validate_json(p.read_text(encoding="utf-8"))
    t.dir = p.parent
    if name == "placeholder" and t.missing_files():
        from services.video_composer.placeholder import generate_placeholder

        return generate_placeholder(templates_dir, fonts=fonts)
    return t


def list_templates(templates_dir: Path) -> list[dict]:
    out = []
    names = {d.name for d in templates_dir.iterdir() if d.is_dir() and (d / "template.json").exists()} if templates_dir.exists() else set()
    names.add("placeholder")
    for name in sorted(names):
        try:
            t = load_template(name, templates_dir)
            out.append({"name": name, "description": t.description, "presets": list(t.layouts), "missing_files": t.missing_files(), "version": t.version})
        except Exception as exc:  # noqa: BLE001 - a broken template must not hide the others
            out.append({"name": name, "error": f"{type(exc).__name__}: {exc}"})
    return out


# --------------------------------------------------------------------------------------------
# geometry
# --------------------------------------------------------------------------------------------

def _even(n: float) -> int:
    v = int(round(n))
    return v if v % 2 == 0 else v - 1


def effective_fit(fit: str, src_w: int, src_h: int, zone: Rect) -> Literal["contain", "cover"]:
    """Resolve "auto" against the real source aspect; contain/cover pass through."""
    if fit != "auto":
        return fit  # type: ignore[return-value]
    if src_w <= 0 or src_h <= 0:
        raise ValueError("source dimensions must be positive")
    scale = max(zone.w / src_w, zone.h / src_h)
    retained = (zone.w * zone.h) / (src_w * scale * src_h * scale)
    return "cover" if retained >= COVER_MIN_RETAINED else "contain"


class CoverCrop(BaseModel):
    """How the source is scaled and cropped to fill a rectangle: scale to (scaled_w, scaled_h), then crop (w, h) at (x, y)."""

    scaled_w: int
    scaled_h: int
    x: int
    y: int
    w: int
    h: int


def cover_crop(src_w: int, src_h: int, rect: Rect, focus_x: float = 0.5, focus_y: float = 0.5) -> CoverCrop:
    """Scale the source up to cover `rect`, then pick the crop window along the overflowing axis.

    focus_x / focus_y (0..1) say which part of the source to keep: 0 = left/top edge, 0.5 = centre, 1 = right/bottom edge.
    Explicit pixel values (not ffmpeg expressions) so the render and the UI preview agree exactly."""
    if src_w <= 0 or src_h <= 0:
        raise ValueError("source dimensions must be positive")
    scale = max(rect.w / src_w, rect.h / src_h)
    sw, sh = max(rect.w, _even(src_w * scale)), max(rect.h, _even(src_h * scale))
    fx, fy = min(max(focus_x, 0.0), 1.0), min(max(focus_y, 0.0), 1.0)
    x = _even((sw - rect.w) * fx)
    y = _even((sh - rect.h) * fy)
    return CoverCrop(scaled_w=sw, scaled_h=sh, x=min(x, sw - rect.w), y=min(y, sh - rect.h), w=rect.w, h=rect.h)


def fit_rect(src_w: int, src_h: int, zone: Rect, fit: str = "contain", align: str = "center") -> Rect:
    """Rectangle the source occupies inside `zone` (even dimensions, centred horizontally)."""
    if src_w <= 0 or src_h <= 0:
        raise ValueError("source dimensions must be positive")
    if effective_fit(fit, src_w, src_h, zone) == "cover":
        return Rect(x=zone.x, y=zone.y, w=_even(zone.w), h=_even(zone.h))
    scale = min(zone.w / src_w, zone.h / src_h)
    w, h = max(2, _even(src_w * scale)), max(2, _even(src_h * scale))
    x = zone.x + (zone.w - w) // 2
    if align == "top":
        y = zone.y
    elif align == "bottom":
        y = zone.bottom - h
    else:
        y = zone.y + (zone.h - h) // 2
    return Rect(x=x, y=y, w=w, h=h)


class ResolvedLayout(BaseModel):
    """Layout after the source aspect ratio is known."""

    preset: str
    width: int
    height: int
    fit: Literal["contain", "cover"]
    video_rect: Rect
    caption_rect: Rect
    caption_anchor: Literal["top", "bottom"]
    captions_over_video: bool
    lower_third_x: int
    lower_third_bottom: int         # the lower-third PNG's bottom edge sits here
    caption_font_size: int
    lower_third_scale: float


def resolve_layout(layout: Layout, preset: str, src_w: int, src_h: int, *, fit: str | None, caption: CaptionStyle,
                   caption_line_px: int, lower_third: LowerThirdStyle) -> ResolvedLayout:
    """Place the clip, the caption area and the lower-third for one source. Pure geometry."""
    fit_eff = effective_fit(fit or layout.fit, src_w, src_h, layout.video_zone)
    video = fit_rect(src_w, src_h, layout.video_zone, fit_eff, layout.video_align)
    font_size = layout.caption_font_size or caption.font_size
    cap_h_max = caption.max_lines * caption_line_px + 2 * caption.padding
    m = layout.caption_margin
    if layout.caption_zone is not None:
        cap = layout.caption_zone
        anchor: Literal["top", "bottom"] = layout.caption_anchor
        over = not (cap.y >= video.bottom or cap.bottom <= video.y)
    else:
        room = layout.video_zone.bottom - video.bottom
        if room >= cap_h_max + layout.caption_gap:
            cap = Rect(x=layout.video_zone.x + m, y=video.bottom + layout.caption_gap, w=layout.video_zone.w - 2 * m, h=room - layout.caption_gap)
            anchor, over = "top", False
        else:
            cap = Rect(x=video.x + m, y=video.bottom - cap_h_max - layout.caption_gap, w=video.w - 2 * m, h=cap_h_max)
            anchor, over = "bottom", True
    if layout.safe_area is not None:   # keep the caption area out of the platform's own UI
        s = layout.safe_area
        cap = Rect(x=max(cap.x, s.x), y=cap.y, w=min(cap.right, s.right) - max(cap.x, s.x), h=cap.h)
    lt_x = video.x + layout.lower_third_margin
    lt_bottom = video.bottom - layout.lower_third_margin
    if over:
        lt_bottom = min(lt_bottom, cap.y - layout.caption_gap)
    return ResolvedLayout(
        preset=preset, width=layout.width, height=layout.height, fit=fit_eff, video_rect=video, caption_rect=cap, caption_anchor=anchor,
        captions_over_video=over, lower_third_x=lt_x, lower_third_bottom=lt_bottom, caption_font_size=font_size,
        lower_third_scale=layout.lower_third_scale,
    )
