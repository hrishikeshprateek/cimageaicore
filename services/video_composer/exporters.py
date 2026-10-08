"""Taking the edit elsewhere: the timeline as FCPXML, EDL, SRT or JSON.

The point is that nothing here is a dead end. The AI proposes the cut, the studio adjusts it, and if the final polish
belongs in Resolve or Premiere the editor opens the same edit there - with every clip pointing at the original file at
the original timecode, not at a re-encoded copy.
"""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from services.video_composer.timeline import Timeline

FPS = 30                       # the timebase the composer renders at
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
NTSC = False                   # 30 exactly, not 29.97: our sources and renders are whole-frame


# ------------------------------------------------------------------ helpers
def timecode(seconds: float, fps: int = FPS) -> str:
    total = int(round(max(0.0, seconds) * fps))
    f = total % fps
    s = (total // fps) % 60
    m = (total // (fps * 60)) % 60
    h = total // (fps * 3600)
    return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"


def srt_time(seconds: float) -> str:
    ms = int(round(max(0.0, seconds) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _rational(seconds: float, fps: int = FPS) -> str:
    """FCPXML wants rational time on the timebase: 2 s at 30 fps is 60/30s."""
    return f"{int(round(seconds * fps))}/{fps}s"


# ------------------------------------------------------------------ EDL (CMX3600) - every editor reads it
def to_edl(t: Timeline, *, title: str | None = None, fps: int = FPS) -> str:
    """CMX3600: one event per clip, source timecode in, record timecode out. Premiere, Resolve and Avid all import it.

    An EDL carries no file paths, only reel names, so each event also gets the file name as a comment - that is how an
    editor relinks it."""
    lines = [f"TITLE: {(title or t.title or 'CIMAGE cut').upper()[:70]}", "FCM: NON-DROP FRAME", ""]
    rec = 0.0
    for n, c in enumerate(t.clips, start=1):
        name = Path(c.source_path or "SLATE").name
        reel = "".join(ch for ch in Path(name).stem.upper() if ch.isalnum())[:8] or f"CLIP{n:03d}"
        src_in, src_out = c.in_seconds, c.out_seconds
        if c.kind != "clip":                     # a still or a slate has no source timecode of its own
            src_in, src_out = 0.0, c.seconds
        lines.append(f"{n:03d}  {reel:<8} V     C        "
                     f"{timecode(src_in, fps)} {timecode(src_out, fps)} {timecode(rec, fps)} {timecode(rec + c.seconds, fps)}")
        lines.append(f"* FROM CLIP NAME: {name}")
        if c.label or c.note:
            lines.append(f"* COMMENT: {(c.label + (' - ' + c.note if c.note else '')).strip()[:200]}")
        if c.track:
            lines.append("* COMMENT: FACE TRACKED - reframe keys in the FCPXML / JSON export")
        rec += c.seconds
        lines.append("")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ FCPXML - Final Cut and DaVinci Resolve
def to_fcpxml(t: Timeline, *, fps: int = FPS, width: int = 1080, height: int = 1920) -> str:
    """FCPXML 1.10: assets for every source file, one spine with the clips in order. Resolve imports this directly
    (File > Import > Timeline), Final Cut opens it as an event."""
    assets: dict[str, dict[str, Any]] = {}
    for c in t.clips:
        if not c.source_path or c.source_path in assets:
            continue
        p = Path(c.source_path)
        still = p.suffix.lower() in IMAGE_SUFFIXES
        assets[c.source_path] = {"id": f"r{len(assets) + 2}", "name": p.stem, "src": p.resolve().as_uri(),
                                 # a still has no intrinsic length or sound; a clip's asset must be at least as long as we use
                                 "duration": 0.0 if still else max(c.out_seconds + 60, 3600.0), "audio": "0" if still else "1"}
    fmt = f'<format id="r1" name="FFVideoFormat{height}p{fps}" frameDuration="1/{fps}s" width="{width}" height="{height}" colorSpace="1-1-1 (Rec. 709)"/>'
    res = [fmt] + [
        f'<asset id="{a["id"]}" name="{html.escape(a["name"])}" start="0s" hasVideo="1" hasAudio="{a["audio"]}" format="r1" '
        f'duration="{_rational(a["duration"], fps)}"><media-rep kind="original-media" src="{html.escape(a["src"])}"/></asset>'
        for a in assets.values()
    ]
    spine, offset = [], 0.0
    for c in t.clips:
        dur, off = _rational(c.seconds, fps), _rational(offset, fps)
        name = html.escape(c.label or Path(c.source_path or "slate").stem)
        if c.source_path and c.source_path in assets:
            el = [f'<asset-clip name="{name}" ref="{assets[c.source_path]["id"]}" offset="{off}" '
                  f'start="{_rational(c.in_seconds, fps)}" duration="{dur}" format="r1" tcFormat="NDF">']
        else:
            el = [f'<gap name="{name}" offset="{off}" start="0s" duration="{dur}">']
        if c.text:
            el.append(f'<title name="{html.escape(c.text[:60])}" offset="{off}" duration="{dur}" ref="r1">'
                      f'<text><text-style>{html.escape(c.text)}</text-style></text></title>')
        for note in ([c.note] if c.note else []) + ([f"face tracked: {len(c.track)} keys"] if c.track else []):
            el.append(f'<note>{html.escape(note[:300])}</note>')
        el.append("</asset-clip>" if (c.source_path and c.source_path in assets) else "</gap>")
        spine.append("\n          ".join(el))
        offset += c.seconds
    project = html.escape(t.title or "CIMAGE cut")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.10">
  <resources>
    {"\n    ".join(res)}
  </resources>
  <library name="CIMAGE AI">
    <event name="CIMAGE AI cuts">
      <project name="{project}">
        <sequence format="r1" duration="{_rational(t.seconds, fps)}" tcStart="0s" tcFormat="NDF" audioLayout="stereo" audioRate="48k">
          <spine>
          {"\n          ".join(spine)}
          </spine>
        </sequence>
      </project>
    </event>
  </library>
</fcpxml>
"""


# ------------------------------------------------------------------ captions + our own format
def to_srt(t: Timeline) -> str:
    """Burned-in captions are baked into the render; this is the sidecar for the editor (and for YouTube)."""
    out, n, base = [], 1, 0.0
    for start, c in zip(t.starts(), t.clips):
        cues = c.captions or ([type("C", (), {"start": 0.0, "end": c.seconds, "text": c.text})()] if c.text else [])
        for cue in cues:
            a, b = base + start + max(0.0, cue.start), base + start + min(c.seconds, cue.end)
            if b <= a:
                continue
            out += [str(n), f"{srt_time(a)} --> {srt_time(b)}", (cue.text or "").strip(), ""]
            n += 1
    return "\n".join(out)


def to_json(t: Timeline) -> str:
    """The edit exactly as we hold it - re-importable here, and readable by any script."""
    return json.dumps(t.describe(), ensure_ascii=False, indent=1)


EXPORTERS = {
    "edl": (to_edl, "text/plain; charset=utf-8", "edl"),
    "fcpxml": (to_fcpxml, "application/xml; charset=utf-8", "fcpxml"),
    "srt": (to_srt, "text/plain; charset=utf-8", "srt"),
    "json": (to_json, "application/json; charset=utf-8", "json"),
}
