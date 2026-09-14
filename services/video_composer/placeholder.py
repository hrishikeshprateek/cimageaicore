"""Generated placeholder template (navy/gold bands with the CIMAGE wordmark) so nothing waits on assets.

Layer geometry follows the real CIMAGE reels frame: top band 0-415, clip window 415-1385, bottom band 1385-1920.
Files land in <templates_dir>/placeholder/ and can be copied as the starting point for a real template.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from services.video_composer.settings import FONTS_DIR
from services.video_composer.template import Brand, Layer, Layout, Rect, Template, save_template
from services.video_composer.textrender import FontPaths, _hex_rgba

BUNDLED_FONTS = FontPaths(
    regular=str(FONTS_DIR / "Poppins-Regular.ttf"), bold=str(FONTS_DIR / "Poppins-SemiBold.ttf"),
    fallback_regular=str(FONTS_DIR / "NotoSansDevanagari-Regular.ttf"), fallback_bold=str(FONTS_DIR / "NotoSansDevanagari-Bold.ttf"),
)


def _band(w: int, h: int, brand: Brand, *, diagonal: str | None = None) -> Image.Image:
    img = Image.new("RGBA", (w, h), _hex_rgba(brand.primary))
    d = ImageDraw.Draw(img)
    if diagonal == "top":       # gold rule along the bottom edge with a slanted notch
        d.rectangle((0, h - 8, w, h), fill=_hex_rgba(brand.accent))
        d.polygon([(w * 0.62, h - 8), (w * 0.66, h - 30), (w, h - 30), (w, h - 8)], fill=_hex_rgba(brand.accent))
    elif diagonal == "bottom":
        d.rectangle((0, 0, w, 8), fill=_hex_rgba(brand.accent))
        d.polygon([(0, 8), (0, 30), (w * 0.34, 30), (w * 0.38, 8)], fill=_hex_rgba(brand.accent))
    return img


def _centered(img: Image.Image, text: str, fonts: FontPaths, weight: str, size: int, y: float, fill) -> None:
    w = fonts.measure(text, weight, size)
    fonts.draw(ImageDraw.Draw(img), ((img.width - w) / 2, y), text, weight, size, fill)


def _wordmark(img: Image.Image, brand: Brand, fonts: FontPaths, *, size: int, y: int, sub: str | None, sub_size: int) -> None:
    _centered(img, brand.short_name, fonts, "bold", size, y, _hex_rgba(brand.accent))
    if sub:
        _centered(img, sub, fonts, "regular", sub_size, y + fonts.line_height("bold", size) + 4, _hex_rgba(brand.text))


def _badges(img: Image.Image, brand: Brand, fonts: FontPaths, labels: list[str], *, y: int, h: int, size: int) -> None:
    """Row of rounded 'accreditation' tiles - stands in for the NAAC / Wipro / Google tiles of the real frame."""
    d = ImageDraw.Draw(img)
    gap = 24
    n = len(labels)
    tile_w = (img.width - gap * (n + 1)) // n
    lh = fonts.line_height("bold", size)
    for i, label in enumerate(labels):
        x0 = gap + i * (tile_w + gap)
        d.rounded_rectangle((x0, y, x0 + tile_w, y + h), radius=14, fill=_hex_rgba(brand.text, 0.12), outline=_hex_rgba(brand.accent, 0.8), width=2)
        tw = fonts.measure(label, "bold", size)
        fonts.draw(d, (x0 + (tile_w - tw) / 2, y + (h - lh) / 2), label, "bold", size, _hex_rgba(brand.text))


def generate_placeholder(templates_dir: Path, *, fonts: FontPaths | None = None, force: bool = False) -> Template:
    fonts = fonts or BUNDLED_FONTS
    brand = Brand()
    out = templates_dir / "placeholder"
    out.mkdir(parents=True, exist_ok=True)
    tiles = ["NAAC ACCREDITED", "WIPRO CENTRE OF EXCELLENCE", "GOOGLE FOR EDUCATION"]

    # ---- reels 1080x1920 : top 0-415, window 415-1385, bottom 1385-1920 (matches the real frame)
    top = _band(1080, 415, brand, diagonal="top")
    _wordmark(top, brand, fonts, size=112, y=36, sub=brand.name.upper(), sub_size=30)
    _badges(top, brand, fonts, tiles, y=262, h=96, size=22)
    bottom = _band(1080, 535, brand, diagonal="bottom")
    _wordmark(bottom, brand, fonts, size=64, y=120, sub="Student Voices", sub_size=40)
    for i, line in enumerate((brand.tagline, brand.contact)):
        _centered(bottom, line, fonts, "regular", 34, 330 + i * 56, _hex_rgba(brand.text, 0.85))
    if force or not (out / "reels_top.png").exists():
        top.save(out / "reels_top.png")
    if force or not (out / "reels_bottom.png").exists():
        bottom.save(out / "reels_bottom.png")

    # ---- square 1080x1080 : top 0-160, window 160-960, bottom 960-1080
    top = _band(1080, 160, brand, diagonal="top")
    _wordmark(top, brand, fonts, size=68, y=18, sub=brand.name.upper(), sub_size=22)
    bottom = _band(1080, 120, brand, diagonal="bottom")
    _centered(bottom, f"{brand.tagline}   ·   {brand.contact}", fonts, "regular", 30, 44, _hex_rgba(brand.text, 0.9))
    if force or not (out / "square_top.png").exists():
        top.save(out / "square_top.png")
    if force or not (out / "square_bottom.png").exists():
        bottom.save(out / "square_bottom.png")

    # ---- landscape 1920x1080 : full-bleed clip, wordmark badge top-left, gradient at the bottom for caption legibility
    badge = Image.new("RGBA", (440, 140), (0, 0, 0, 0))
    ImageDraw.Draw(badge).rounded_rectangle((0, 0, 439, 139), radius=16, fill=_hex_rgba(brand.primary, 0.9), outline=_hex_rgba(brand.accent), width=3)
    _wordmark(badge, brand, fonts, size=54, y=10, sub=brand.tagline, sub_size=20)
    r, g, b, _ = _hex_rgba(brand.primary)
    column = Image.new("RGBA", (1, 260), (0, 0, 0, 0))
    column.putdata([(r, g, b, int(200 * (y / 259) ** 1.4)) for y in range(260)])
    grad = column.resize((1920, 260), Image.Resampling.NEAREST)
    if force or not (out / "landscape_badge.png").exists():
        badge.save(out / "landscape_badge.png")
    if force or not (out / "landscape_gradient.png").exists():
        grad.save(out / "landscape_gradient.png")

    t = Template(
        name="placeholder",
        description="Generated stand-in for the CIMAGE frame (same geometry as the real 9:16 template). Replace the PNGs or upload real layers.",
        brand=brand,
        layouts={
            "reels": Layout(
                width=1080, height=1920, video_zone=Rect(x=0, y=440, w=1080, h=920), video_align="top", background=brand.primary,
                layers=[Layer(name="top", file="reels_top.png", x=0, y=0, z=0), Layer(name="bottom", file="reels_bottom.png", x=0, y=1385, z=1)],
                safe_area=Rect(x=60, y=250, w=860, h=1350), caption_font_size=54,
            ),
            "square": Layout(
                width=1080, height=1080, video_zone=Rect(x=0, y=160, w=1080, h=800), video_align="top", background=brand.primary,
                layers=[Layer(name="top", file="square_top.png", x=0, y=0, z=0), Layer(name="bottom", file="square_bottom.png", x=0, y=960, z=1)],
                caption_font_size=40,
            ),
            "landscape": Layout(
                width=1920, height=1080, video_zone=Rect(x=0, y=0, w=1920, h=1080), fit="contain",
                layers=[Layer(name="gradient", file="landscape_gradient.png", x=0, y=820, z=0), Layer(name="badge", file="landscape_badge.png", x=40, y=40, z=1)],
                caption_zone=Rect(x=160, y=840, w=1600, h=200), caption_anchor="bottom", caption_font_size=52, lower_third_margin=48,
            ),
        },
    )
    save_template(t, templates_dir)
    return t
