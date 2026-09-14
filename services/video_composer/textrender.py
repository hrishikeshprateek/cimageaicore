"""Text -> alpha PNG with Pillow (captions, lower-thirds, placeholder art).

Why PNG overlays and not ffmpeg drawtext/libass: the ffmpeg on the dev machine is built without
libfreetype/libass, and PNG overlays behave identically on every ffmpeg build.

Scripts: text is split into script runs (Devanagari vs everything else) and each run is drawn with
the primary font when it has the glyphs, otherwise with the Devanagari fallback - so a Latin-only
brand font can arrive later without breaking Hindi captions. Devanagari needs a shaping engine
(conjuncts, matras): Pillow uses libraqm when FriBiDi can be found. On Apple-silicon Homebrew,
FriBiDi lives in /opt/homebrew/lib where Pillow's bundled raqm does not look, so when in-process
shaping is unavailable we render in a child interpreter with DYLD_FALLBACK_LIBRARY_PATH set.

    python -m services.video_composer.textrender -  < jobs.json      (what the child process runs)
"""
from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Literal

from PIL import Image, ImageDraw, ImageFont, features
from pydantic import BaseModel, Field

from services.video_composer.template import CaptionStyle, LowerThirdStyle

log = logging.getLogger(__name__)

Weight = Literal["regular", "bold"]
_DEVA_RANGES = ((0x0900, 0x097F), (0xA8E0, 0xA8FF), (0x1CD0, 0x1CFF))
_COMPLEX_RANGES = _DEVA_RANGES + ((0x0980, 0x0DFF), (0x0590, 0x08FF), (0x0E00, 0x0E7F), (0x1000, 0x109F))   # other Indic, Hebrew/Arabic, Thai, Myanmar
_NEUTRAL = set(" \t‌‍.,;:!?'\"()-–—/0123456789%₹&+·|")
_FRIBIDI_CANDIDATES = (Path("/opt/homebrew/lib"), Path("/usr/local/lib"), Path("/opt/local/lib"))
_PROBE = {"deva": "क", "latin": "A"}


def needs_shaping(text: str) -> bool:
    return any(lo <= ord(ch) <= hi for ch in text for lo, hi in _COMPLEX_RANGES)


def shaping_available() -> bool:
    return bool(features.check("raqm"))


def fribidi_dir(override: Path | None = None) -> Path | None:
    for d in ([override] if override else []) + list(_FRIBIDI_CANDIDATES):
        if d and any((d / n).exists() for n in ("libfribidi.dylib", "libfribidi.0.dylib", "libfribidi.so.0", "libfribidi.so")):
            return d
    return None


def shaping_status(override: Path | None = None) -> dict:
    d = fribidi_dir(override)
    return {
        "raqm_in_process": shaping_available(),
        "fribidi_dir": str(d) if d else None,
        "subprocess_fallback": (not shaping_available()) and d is not None and platform.system() == "Darwin",
        "harfbuzz": features.version("harfbuzz"),
        "pillow": features.version("pil"),
    }


def _hex_rgba(colour: str, opacity: float = 1.0) -> tuple[int, int, int, int]:
    c = colour.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    a = int(c[6:8], 16) if len(c) == 8 else int(round(255 * opacity))
    return r, g, b, a


@lru_cache(maxsize=128)
def load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    engine = ImageFont.Layout.RAQM if shaping_available() else ImageFont.Layout.BASIC
    return ImageFont.truetype(path, size, layout_engine=engine)


@lru_cache(maxsize=256)
def has_glyph(path: str, ch: str) -> bool:
    """Does the font draw `ch` with something other than .notdef? (Pillow exposes no cmap, so compare masks.)"""
    font = load_font(path, 32)
    return bytes(font.getmask(ch)) != bytes(font.getmask("͸")) or font.getmask(ch).size != font.getmask("͸").size


def _script(ch: str) -> str | None:
    if ch in _NEUTRAL:
        return None
    o = ord(ch)
    return "deva" if any(lo <= o <= hi for lo, hi in _DEVA_RANGES) else "latin"


def script_runs(text: str) -> list[tuple[str, str]]:
    """[(script, run)] - neutral characters (spaces, digits, punctuation) stay with the run they follow."""
    runs: list[tuple[str, str]] = []
    cur_script: str | None = None
    cur = ""
    for ch in text:
        s = _script(ch)
        if s is None or s == cur_script or cur_script is None:
            cur += ch
            if s is not None and cur_script is None:
                cur_script = s
        else:
            runs.append((cur_script, cur))
            cur_script, cur = s, ch
    if cur:
        runs.append((cur_script or "latin", cur))
    return runs


class FontPaths(BaseModel):
    regular: str
    bold: str
    fallback_regular: str | None = None
    fallback_bold: str | None = None

    def path(self, weight: Weight, script: str) -> str:
        primary = self.regular if weight == "regular" else self.bold
        fallback = (self.fallback_regular if weight == "regular" else self.fallback_bold) or primary
        if has_glyph(primary, _PROBE[script]):
            return primary
        return fallback if has_glyph(fallback, _PROBE[script]) else primary

    def metrics(self, weight: Weight, size: int) -> tuple[int, int]:
        """(ascent, descent) - the larger of primary and fallback so mixed lines never clip."""
        paths = {self.regular if weight == "regular" else self.bold, (self.fallback_regular if weight == "regular" else self.fallback_bold) or ""}
        asc = desc = 0
        for p in paths:
            if p:
                a, d = load_font(p, size).getmetrics()
                asc, desc = max(asc, a), max(desc, d)
        return asc, desc

    def line_height(self, weight: Weight, size: int, multiplier: float = 1.0) -> int:
        a, d = self.metrics(weight, size)
        return int(round((a + d) * multiplier))

    def measure(self, text: str, weight: Weight, size: int) -> float:
        return sum(load_font(self.path(weight, s), size).getlength(run) for s, run in script_runs(text))

    def draw(self, draw: ImageDraw.ImageDraw, xy: tuple[float, float], text: str, weight: Weight, size: int, fill, *, stroke_width: int = 0, stroke_fill=None) -> float:
        """Draw one line run by run at (x, ascender-line y); returns the drawn width."""
        x, y = xy
        asc, _ = self.metrics(weight, size)
        for s, run in script_runs(text):
            font = load_font(self.path(weight, s), size)
            a, _ = font.getmetrics()
            kw = {"stroke_width": stroke_width, "stroke_fill": stroke_fill} if stroke_width and stroke_fill is not None else {}
            draw.text((x, y + (asc - a)), run, font=font, fill=fill, anchor="la", **kw)
            x += font.getlength(run)
        return x - xy[0]


def wrap_text(text: str, fonts: FontPaths, weight: Weight, size: int, max_width: int) -> list[str]:
    """Greedy word wrap on spaces (Hindi and English both use spaces); over-long words are kept whole."""
    lines: list[str] = []
    for para in text.replace("\r", "").split("\n"):
        words = para.split()
        cur = ""
        for w in words:
            cand = f"{cur} {w}".strip()
            if cur and fonts.measure(cand, weight, size) > max_width:
                lines.append(cur)
                cur = w
            else:
                cur = cand
        if cur:
            lines.append(cur)
    return lines or [""]


# --------------------------------------------------------------------------------------------
# job specs (JSON-serialisable so a child process can run them)
# --------------------------------------------------------------------------------------------

class CaptionJob(BaseModel):
    kind: Literal["caption"] = "caption"
    text: str
    width: int                       # available width (the caption area); the PNG is exactly this wide
    style: CaptionStyle
    font_size: int
    fonts: FontPaths
    out: str


class LowerThirdJob(BaseModel):
    kind: Literal["lower_third"] = "lower_third"
    name: str
    role: str | None = None
    style: LowerThirdStyle
    scale: float = 1.0
    fonts: FontPaths
    out: str


class TextResult(BaseModel):
    out: str
    width: int
    height: int
    lines: int = 1
    font_size: int


TextJob = CaptionJob | LowerThirdJob


class TextJobs(BaseModel):
    jobs: list[TextJob] = Field(default_factory=list)


def render_caption(job: CaptionJob) -> TextResult:
    st = job.style
    weight: Weight = "bold" if st.bold else "regular"
    size = job.font_size
    inner_w = job.width - 2 * st.padding
    while True:
        lines = wrap_text(job.text, job.fonts, weight, size, inner_w)
        widest = max(job.fonts.measure(ln, weight, size) for ln in lines)
        if (len(lines) <= st.max_lines and widest <= inner_w) or size <= max(18, int(job.font_size * 0.6)):
            break
        size -= 2
    lh = job.fonts.line_height(weight, size, st.line_height)
    box_w = int(min(job.width, widest + 2 * st.padding))
    box_h = lh * len(lines) + 2 * st.padding
    img = Image.new("RGBA", (job.width, box_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    box_x = (job.width - box_w) // 2 if st.align == "center" else 0
    if st.box_opacity > 0:
        draw.rounded_rectangle((box_x, 0, box_x + box_w - 1, box_h - 1), radius=st.box_radius, fill=_hex_rgba(st.box_colour, st.box_opacity))
    natural = sum(job.fonts.metrics(weight, size))
    stroke = {"stroke_width": st.stroke_width, "stroke_fill": _hex_rgba(st.stroke_colour)} if st.stroke_width and st.stroke_colour else {}
    for i, ln in enumerate(lines):
        w = job.fonts.measure(ln, weight, size)
        x = box_x + (box_w - w) / 2 if st.align == "center" else box_x + st.padding
        y = st.padding + i * lh + (lh - natural) / 2
        job.fonts.draw(draw, (x, y), ln, weight, size, _hex_rgba(st.colour), **stroke)
    Path(job.out).parent.mkdir(parents=True, exist_ok=True)
    img.save(job.out, "PNG", optimize=False)
    return TextResult(out=job.out, width=img.width, height=img.height, lines=len(lines), font_size=size)


def render_lower_third(job: LowerThirdJob) -> TextResult:
    st = job.style
    s = job.scale
    name_size, role_size = int(st.name_size * s), int(st.role_size * s)
    pad, accent, radius = int(st.padding * s), int(st.accent_width * s), int(st.radius * s)
    max_w = int(st.max_width * s)
    inner = max_w - 2 * pad - accent
    name_lines = wrap_text(job.name, job.fonts, "bold", name_size, inner)[:2]
    role_lines = wrap_text(job.role, job.fonts, "regular", role_size, inner)[:2] if job.role else []
    name_lh = job.fonts.line_height("bold", name_size)
    role_lh = job.fonts.line_height("regular", role_size)
    gap = int(4 * s) if role_lines else 0
    text_w = max([job.fonts.measure(x, "bold", name_size) for x in name_lines] + [job.fonts.measure(x, "regular", role_size) for x in role_lines])
    box_w = int(min(max_w, text_w + 2 * pad + accent))
    box_h = name_lh * len(name_lines) + role_lh * len(role_lines) + 2 * pad + gap
    img = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, box_w - 1, box_h - 1), radius=radius, fill=_hex_rgba(st.bg_colour, st.bg_opacity))
    draw.rounded_rectangle((0, 0, accent + radius, box_h - 1), radius=radius, fill=_hex_rgba(st.accent_colour))
    draw.rectangle((accent, 0, accent + radius, box_h - 1), fill=_hex_rgba(st.bg_colour, st.bg_opacity))
    x, y = accent + pad, pad
    for ln in name_lines:
        job.fonts.draw(draw, (x, y), ln, "bold", name_size, _hex_rgba(st.colour))
        y += name_lh
    y += gap
    for ln in role_lines:
        job.fonts.draw(draw, (x, y), ln, "regular", role_size, _hex_rgba(st.colour, 0.88))
        y += role_lh
    Path(job.out).parent.mkdir(parents=True, exist_ok=True)
    img.save(job.out, "PNG", optimize=False)
    return TextResult(out=job.out, width=img.width, height=img.height, lines=len(name_lines) + len(role_lines), font_size=name_size)


def render_jobs_in_process(jobs: list[TextJob]) -> list[TextResult]:
    return [render_caption(j) if isinstance(j, CaptionJob) else render_lower_third(j) for j in jobs]


def render_jobs(jobs: list[TextJob], *, fribidi_override: Path | None = None) -> tuple[list[TextResult], bool]:
    """Render in-process, or in a child interpreter that can load FriBiDi (macOS/Homebrew) when a job needs shaping.

    Returns (results, shaped) where `shaped` says whether a shaping engine (raqm) laid out the text.
    """
    if not jobs:
        return [], shaping_available()
    complex_text = any(needs_shaping(j.text if isinstance(j, CaptionJob) else f"{j.name} {j.role or ''}") for j in jobs)
    d = fribidi_dir(fribidi_override)
    if complex_text and not shaping_available() and d is not None and platform.system() == "Darwin":
        return _render_in_child(jobs, d)
    if complex_text and not shaping_available():
        log.warning("complex-script text without a shaping engine (install fribidi so Pillow can use libraqm)")
    return render_jobs_in_process(jobs), shaping_available()


def _render_in_child(jobs: list[TextJob], lib_dir: Path) -> tuple[list[TextResult], bool]:
    spec = TextJobs(jobs=jobs).model_dump_json()
    env = dict(os.environ, DYLD_FALLBACK_LIBRARY_PATH=f"{lib_dir}:{os.environ.get('DYLD_FALLBACK_LIBRARY_PATH', '')}".rstrip(":"))
    repo_root = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = repo_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    proc = subprocess.run([sys.executable, "-m", "services.video_composer.textrender", "-"], input=spec, capture_output=True, text=True,
                          env=env, cwd=repo_root, timeout=120, check=False)
    if proc.returncode != 0:
        log.warning("child text renderer failed (%s), rendering without shaping: %s", proc.returncode, proc.stderr[-500:])
        return render_jobs_in_process(jobs), False
    data = json.loads(proc.stdout)
    if not data.get("shaped"):
        log.warning("child text renderer could not enable shaping either")
    return [TextResult.model_validate(r) for r in data["results"]], bool(data.get("shaped"))


def _main(argv: list[str]) -> int:
    raw = sys.stdin.read() if argv[1:2] == ["-"] else Path(argv[1]).read_text(encoding="utf-8")
    jobs = TextJobs.model_validate_json(raw).jobs
    results = render_jobs_in_process(jobs)
    print(json.dumps({"shaped": shaping_available(), "results": [r.model_dump() for r in results]}))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through render_jobs()
    sys.exit(_main(sys.argv))
