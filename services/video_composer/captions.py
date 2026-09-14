"""Transcript blocks -> caption cues (short, readable chunks) -> SRT / ASS sidecars.

Transcript segments are 30-60 s speaker turns with second-resolution timestamps, far too long
for burned-in captions, so each segment is split into chunks that fit `max_lines x max_chars`
and the segment's time is distributed over the chunks in proportion to their length.
Cue times are relative to the cut's in-point.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from pydantic import BaseModel, Field

from services.block_engine.media import ts_to_seconds
from services.block_engine.schemas import TranscriptSegment

_SENTENCE_END = re.compile(r"(?<=[।.!?॥])\s+")
_GENERIC_SPEAKER = re.compile(r"^(speaker\s*\d*|audience|voice-?over|narrator|unknown|host|interviewer)$", re.IGNORECASE)


class CaptionCue(BaseModel):
    start: float = Field(ge=0)
    end: float
    text: str
    speaker: str | None = None

    @property
    def duration(self) -> float:
        return self.end - self.start


def is_generic_speaker(name: str | None) -> bool:
    return not name or bool(_GENERIC_SPEAKER.match(name.strip()))


def chunk_text(text: str, max_chars: int) -> list[str]:
    """Split into chunks of at most `max_chars`, preferring sentence ends, then word boundaries."""
    text = " ".join(text.split())
    if not text:
        return []
    chunks: list[str] = []
    for sentence in _SENTENCE_END.split(text):
        words = sentence.split()
        cur = ""
        for w in words:
            cand = f"{cur} {w}".strip()
            if cur and len(cand) > max_chars:
                chunks.append(cur)
                cur = w
            else:
                cur = cand
        if cur:
            # glue a very short trailing sentence onto the previous chunk when it fits
            if chunks and len(cur) < max_chars // 3 and len(chunks[-1]) + 1 + len(cur) <= max_chars:
                chunks[-1] = f"{chunks[-1]} {cur}"
            else:
                chunks.append(cur)
    return chunks


def _seg_bounds(seg: TranscriptSegment) -> tuple[float, float] | None:
    s, e = ts_to_seconds(seg.start_time), ts_to_seconds(seg.end_time)
    if s is None or e is None:
        return None
    if e <= s:
        e = s + max(2.0, len(seg.text.split()) / 2.4)   # zero-length segment: estimate from speech rate
    return float(s), float(e)


def cues_for_window(segments: Iterable[TranscriptSegment], cut_in: float, cut_out: float, *, max_chars_per_line: int = 34,
                    max_lines: int = 2, min_seconds: float = 0.8) -> list[CaptionCue]:
    """Caption cues (times relative to `cut_in`) for the transcript that overlaps [cut_in, cut_out]."""
    max_chars = max_chars_per_line * max_lines
    cues: list[CaptionCue] = []
    for seg in segments:
        b = _seg_bounds(seg)
        if b is None:
            continue
        s, e = b
        if e <= cut_in or s >= cut_out:
            continue
        chunks = chunk_text(seg.text, max_chars)
        if not chunks:
            continue
        total = sum(len(c) for c in chunks)
        t = s
        for c in chunks:
            d = (e - s) * len(c) / total
            c_start, c_end = t, t + d
            t = c_end
            vs, ve = max(c_start, cut_in), min(c_end, cut_out)
            if ve - vs < min(min_seconds, d) * 0.5:      # only a sliver of this cue is inside the cut
                continue
            cues.append(CaptionCue(start=round(vs - cut_in, 3), end=round(ve - cut_in, 3), text=c, speaker=None if is_generic_speaker(seg.speaker) else seg.speaker))
    cues.sort(key=lambda c: c.start)
    # never overlap: a later cue starts where the previous one ends at the latest
    for prev, cur in zip(cues, cues[1:]):
        if cur.start < prev.end:
            prev.end = cur.start
    return [c for c in cues if c.end - c.start > 0.05]


# --------------------------------------------------------------------------------------------
# sidecars
# --------------------------------------------------------------------------------------------

def _srt_ts(t: float) -> str:
    ms = int(round(t * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _ass_ts(t: float) -> str:
    cs = int(round(t * 100))
    h, rem = divmod(cs, 360_000)
    m, rem = divmod(rem, 6_000)
    s, cs = divmod(rem, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def to_srt(cues: list[CaptionCue]) -> str:
    out = []
    for i, c in enumerate(cues, 1):
        out.append(f"{i}\n{_srt_ts(c.start)} --> {_srt_ts(c.end)}\n{c.text}\n")
    return "\n".join(out) + ("\n" if out else "")


def to_ass(cues: list[CaptionCue], *, width: int, height: int, font_name: str = "Noto Sans Devanagari", font_size: int = 54,
           margin_v: int = 120, colour: str = "#ffffff", box_colour: str = "#000000", box_opacity: float = 0.6) -> str:
    """ASS with an opaque-box style (BorderStyle=3). Used by the optional libass caption engine and as a sidecar."""
    def ass_colour(hex_colour: str, opacity: float = 1.0) -> str:
        c = hex_colour.lstrip("#")
        r, g, b = c[0:2], c[2:4], c[4:6]
        a = int(round(255 * (1 - opacity)))
        return f"&H{a:02X}{b}{g}{r}".upper()

    head = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: {w}\nPlayResY: {h}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Caption,{font},{size},{primary},{primary},{back},{back},0,0,0,0,100,100,0,0,3,14,0,2,60,60,{mv},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    ).format(w=width, h=height, font=font_name, size=font_size, primary=ass_colour(colour), back=ass_colour(box_colour, box_opacity), mv=margin_v)
    lines = [f"Dialogue: 0,{_ass_ts(c.start)},{_ass_ts(c.end)},Caption,,0,0,0,,{c.text.replace(chr(10), chr(92) + 'N')}" for c in cues]
    return head + "\n".join(lines) + ("\n" if lines else "")


def write_sidecars(cues: list[CaptionCue], base: Path, **ass_kwargs) -> tuple[Path, Path]:
    srt, ass = base.with_suffix(".srt"), base.with_suffix(".ass")
    srt.write_text(to_srt(cues), encoding="utf-8")
    ass.write_text(to_ass(cues, **ass_kwargs), encoding="utf-8")
    return srt, ass
