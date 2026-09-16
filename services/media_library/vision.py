"""Visual index of an analysed video: shot detection -> numbered contact sheets -> the AI Gateway says what each still
actually shows (the analysis timestamps are approximate) -> only usable, accurately described stills become candidates."""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Literal

from PIL import Image, ImageDraw
from pydantic import BaseModel, Field, ValidationError

from services.block_engine.media import seconds_to_ts, ts_to_seconds
from services.block_engine.schemas import provider_json_schema
from services.media_library.frames import FrameError, extract_frame, sharpness

log = logging.getLogger(__name__)
FFMPEG = shutil.which("ffmpeg")
PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts" / "content-generation"
FONT = Path(__file__).resolve().parents[1] / "video_composer" / "assets" / "fonts" / "Poppins-SemiBold.ttf"
SHEET_COLS, SHEET_ROWS, TILE = 3, 3, (600, 338)
Quality = Literal["good", "blurry", "transition", "text_only", "duplicate"]
Suitability = Literal["blog_hero", "social_post", "thumbnail", "press", "archive"]


class ShotTile(BaseModel):
    tile: int
    visible: str = Field(description="One factual sentence: what is visible in this still.")
    people: int = Field(description="Approximate number of people visible (0 if none).")
    quality: Quality
    suitable_for: list[Suitability] = Field(default_factory=list)
    matches_block: str | None = Field(default=None, description="Knowledge block id this still clearly shows, or null.")


class ShotSheetV1(BaseModel):
    tiles: list[ShotTile]


class Shot(BaseModel):
    time: float
    description: str = ""
    people: int = 0
    quality: str = "unknown"
    suitable_for: list[str] = Field(default_factory=list)
    block_id: str | None = None
    sharpness: float = 0.0


# --------------------------------------------------------------------------------------------
# shots
# --------------------------------------------------------------------------------------------

def detect_shots(video: Path, *, duration: float | None, threshold: float = 0.3, min_gap: float = 1.2, max_shots: int = 60) -> list[float]:
    """Scene-change timestamps (+ regular samples so static videos still yield a few stills)."""
    if FFMPEG is None:
        raise FrameError("ffmpeg not found on PATH")
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-nostats", "-i", str(video), "-vf", f"select='gt(scene,{threshold})',showinfo", "-an", "-f", "null", "-"],
        capture_output=True, text=True, timeout=900, check=False,
    )
    times = sorted({round(float(t), 2) for t in re.findall(r"pts_time:\s*([0-9.]+)", proc.stderr)})
    dur = duration or (times[-1] + 1 if times else 0)
    # a still just inside the start, and regular samples every ~8 s when cuts are sparse
    step = max(6.0, (dur or 60) / 12)
    extra = [0.5] + [round(t, 2) for t in _frange(step, (dur or 0) - 0.5, step)]
    merged: list[float] = []
    for t in sorted(times + extra):
        if t < 0 or (dur and t > dur - 0.2):
            continue
        if merged and t - merged[-1] < min_gap:
            continue
        merged.append(t + 0.35 if t in times else t)   # a beat after the cut, past the first (often blended) frame
    if len(merged) > max_shots:    # keep an even spread
        stride = len(merged) / max_shots
        merged = [merged[int(i * stride)] for i in range(max_shots)]
    return merged


def _frange(start: float, stop: float, step: float):
    t = start
    while t < stop:
        yield t
        t += step


def ahash(path: Path) -> int:
    with Image.open(path) as im:
        g = im.convert("L").resize((8, 8), Image.Resampling.LANCZOS)
        px = list(g.tobytes())
        avg = sum(px) / 64
        return sum(1 << i for i, v in enumerate(px) if v > avg)


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


# --------------------------------------------------------------------------------------------
# contact sheets
# --------------------------------------------------------------------------------------------

def contact_sheet(stills: list[tuple[int, float, Path]], out: Path) -> Path:
    """Grid of numbered tiles ('3 · 00:01:05'), 3x3 at 600x338."""
    from PIL import ImageFont

    rows = (len(stills) + SHEET_COLS - 1) // SHEET_COLS
    sheet = Image.new("RGB", (SHEET_COLS * TILE[0], rows * TILE[1]), (12, 12, 12))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.truetype(str(FONT), 30) if FONT.exists() else ImageFont.load_default()
    for k, (n, t, path) in enumerate(stills):
        with Image.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail(TILE)
            x = (k % SHEET_COLS) * TILE[0] + (TILE[0] - im.width) // 2
            y = (k // SHEET_COLS) * TILE[1] + (TILE[1] - im.height) // 2
            sheet.paste(im, (x, y))
        label = f"{n} · {seconds_to_ts(t)}"
        lx, ly = (k % SHEET_COLS) * TILE[0] + 10, (k // SHEET_COLS) * TILE[1] + 8
        w = draw.textlength(label, font=font)
        draw.rectangle((lx - 6, ly - 4, lx + w + 8, ly + 38), fill=(0, 0, 0))
        draw.text((lx, ly), label, font=font, fill=(255, 210, 60))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, "JPEG", quality=85)
    return out


def load_prompt(version: str | None = None) -> tuple[str, str]:
    """prompts/content-generation/shots_*.md via the registry (active version when None; UI-saved versions win)."""
    from services import prompts
    return prompts.prompt("shots", version)


def _fill(t: str, values: dict[str, str]) -> str:
    for k, v in values.items():
        t = t.replace("{" + k + "}", v)
    return t


# --------------------------------------------------------------------------------------------
# the pass
# --------------------------------------------------------------------------------------------

def index_shots(video: Path, *, duration: float | None, source_name: str, blocks: list, provider, institution_context: str = "",
                prompt_version: str | None = None, model: str | None = None, max_shots: int = 60, dedupe_distance: int = 6) -> tuple[list[Shot], dict[str, Any]]:
    """Detect shots, describe them through the gateway, return usable shots (+ usage/meta). Never raises on the AI side: a failed
    sheet leaves its shots undescribed (quality 'unknown')."""
    times = detect_shots(video, duration=duration, max_shots=max_shots)
    system, user = load_prompt(prompt_version)
    described: list[Shot] = []
    usage: dict[str, int] = {}
    sheets = 0
    with tempfile.TemporaryDirectory() as tmp:
        stills: list[tuple[int, float, Path]] = []
        hashes: list[int] = []
        n = 0
        for t in times:   # one sharp still per shot, near-duplicates dropped before anything is sent
            p = Path(tmp) / f"s{len(stills):03d}.jpg"
            try:
                extract_frame(video, t, p, max_width=960, duration=duration, offsets=(0.0, 0.5))
            except FrameError:
                continue
            h = ahash(p)
            if any(_hamming(h, other) <= dedupe_distance for other in hashes):
                continue
            hashes.append(h)
            n += 1
            stills.append((n, t, p))
        near = [b for b in blocks if b.block_type in ("media", "key_moment") and b.timestamp]
        for start in range(0, len(stills), SHEET_COLS * SHEET_ROWS):
            batch = stills[start:start + SHEET_COLS * SHEET_ROWS]
            sheet = contact_sheet(batch, Path(tmp) / f"sheet{sheets}.jpg")
            sheets += 1
            lo, hi = batch[0][1] - 12, batch[-1][1] + 12
            block_lines = [f"- [id={b.block_id}] @ {b.timestamp} ({b.block_type}) {b.text}" for b in near if lo <= (ts_to_seconds(b.timestamp) or -1) <= hi] or ["- (none)"]
            values = {"institution_context": institution_context, "source_name": source_name,
                      "tiles": "\n".join(f"- tile {k} -> {seconds_to_ts(t)}" for k, t, _ in batch), "blocks": "\n".join(block_lines)}
            try:
                raw = provider.describe_images(_fill(system, values), _fill(user, values), [(sheet.read_bytes(), "image/jpeg")],
                                               provider_json_schema(ShotSheetV1), model=model)
                parsed = ShotSheetV1.model_validate(json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.text.strip())))
                for k, v in (raw.usage or {}).items():
                    if v:
                        usage[k] = usage.get(k, 0) + v
            except (ValidationError, json.JSONDecodeError, Exception) as exc:  # noqa: BLE001 - one bad sheet must not sink the pass
                log.warning("shot sheet %d not described: %s", sheets, exc)
                described += [Shot(time=t, sharpness=sharpness(p)) for _, t, p in batch]
                continue
            by_tile = {x.tile: x for x in parsed.tiles}
            for k, t, p in batch:
                x = by_tile.get(k)
                if x is None:
                    described.append(Shot(time=t, sharpness=sharpness(p)))
                    continue
                block_id = x.matches_block if x.matches_block and any(b.block_id == x.matches_block for b in near) else None
                described.append(Shot(time=t, description=x.visible.strip(), people=x.people, quality=x.quality, suitable_for=list(dict.fromkeys(x.suitable_for)),
                                      block_id=block_id, sharpness=sharpness(p)))
    return described, {"shots_detected": len(times), "stills": len(described), "sheets": sheets, "usage": usage, "model": model or getattr(provider, "model", None)}


# --------------------------------------------------------------------------------------------
# YouTube thumbnails (the institution's own channel; used when a job has no local file)
# --------------------------------------------------------------------------------------------

_YT_ID = re.compile(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})")


def youtube_video_id(url: str) -> str | None:
    m = _YT_ID.search(url or "")
    return m.group(1) if m else None


def fetch_youtube_thumbnail(url: str, out: Path, *, timeout: float = 15) -> Path | None:
    """maxresdefault.jpg when it exists (1280x720), else hqdefault.jpg (480x360). Returns None on any failure."""
    import httpx

    vid = youtube_video_id(url)
    if not vid:
        return None
    for name in ("maxresdefault.jpg", "sddefault.jpg", "hqdefault.jpg"):
        try:
            r = httpx.get(f"https://img.youtube.com/vi/{vid}/{name}", timeout=timeout, follow_redirects=True)
        except httpx.HTTPError as exc:
            log.warning("youtube thumbnail %s: %s", name, exc)
            continue
        if r.status_code == 200 and len(r.content) > 2000:   # YouTube serves a tiny grey placeholder for missing sizes
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(r.content)
            return out
    return None
