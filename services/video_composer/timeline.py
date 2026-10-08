"""The edit itself: an ordered list of clips with their framing, captions and audio.

Everything the studio shows and everything the renderer renders is one of these. The AI fills it in (cut proposals, a
script's scenes, faces it tracked); a person then moves a handle and it is still the same object. It is also what gets
exported, so an edit can leave for Resolve or Premiere and come back as a finished film.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from services.video_composer.captions import CaptionCue
from services.video_composer.renderer import LowerThird

ClipKind = Literal["clip", "still", "slate"]
AudioMode = Literal["source", "voiceover", "both"]


def _id() -> str:
    return uuid.uuid4().hex[:8]


class FocusKey(BaseModel):
    """Where the crop window looks at a moment in time (normalised to the source frame, 0..1)."""

    t: float = Field(ge=0, description="seconds from the start of the clip")
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)


class Clip(BaseModel):
    id: str = Field(default_factory=_id)
    kind: ClipKind = "clip"
    job_id: str | None = None                 # which analysed video this came from (None for a still or a slate)
    source_path: str | None = None
    in_seconds: float = 0.0                   # position in the source
    out_seconds: float = 0.0
    source_width: int | None = None
    source_height: int | None = None
    # framing: a static focus, or a path the crop follows (face tracking). Keys are relative to the clip's start.
    focus_x: float = Field(0.5, ge=0, le=1)
    focus_y: float = Field(0.5, ge=0, le=1)
    track: list[FocusKey] = Field(default_factory=list)
    fit: Literal["contain", "cover", "auto"] | None = None
    # what is on top of it
    text: str | None = None                   # on-screen text for the whole clip
    captions: list[CaptionCue] = Field(default_factory=list)
    lower_third: LowerThird | None = None
    # audio
    mute: bool = False
    gain_db: float = 0.0
    voice_path: str | None = None             # a spoken line that belongs to this clip
    # provenance, shown in the studio
    label: str = ""
    sentences: list[int] = Field(default_factory=list)     # transcript sentence ids this clip covers
    note: str = ""

    @property
    def seconds(self) -> float:
        return round(max(0.0, self.out_seconds - self.in_seconds), 3)

    def with_window(self, a: float, b: float) -> "Clip":
        return self.model_copy(update={"in_seconds": round(a, 3), "out_seconds": round(b, 3)})


class Timeline(BaseModel):
    id: str = Field(default_factory=_id)
    title: str = ""
    preset: Literal["reels", "square", "landscape"] = "reels"
    template: str | None = None
    fit: Literal["contain", "cover", "auto"] | None = None
    audio: AudioMode = "source"
    clips: list[Clip] = Field(default_factory=list)
    source: dict[str, Any] = Field(default_factory=dict)   # where it came from: {"kind": "cut"|"script", "id": ...}
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def seconds(self) -> float:
        return round(sum(c.seconds for c in self.clips), 3)

    @property
    def jobs(self) -> list[str]:
        """Every analysed video this edit draws on, in order of first use."""
        out: list[str] = []
        for c in self.clips:
            if c.job_id and c.job_id not in out:
                out.append(c.job_id)
        return out

    def starts(self) -> list[float]:
        """Where each clip begins on the finished timeline."""
        t, out = 0.0, []
        for c in self.clips:
            out.append(round(t, 3))
            t += c.seconds
        return out

    def at(self, t: float) -> Clip | None:
        for start, c in zip(self.starts(), self.clips):
            if start <= t < start + c.seconds:
                return c
        return self.clips[-1] if self.clips else None

    def move(self, clip_id: str, to: int) -> "Timeline":
        i = next((n for n, c in enumerate(self.clips) if c.id == clip_id), None)
        if i is None:
            return self
        c = self.clips.pop(i)
        self.clips.insert(max(0, min(to, len(self.clips))), c)
        return self

    def drop(self, clip_id: str) -> "Timeline":
        self.clips = [c for c in self.clips if c.id != clip_id]
        return self

    def describe(self) -> dict[str, Any]:
        """What the studio draws: every clip with its position on the finished timeline."""
        return {
            "id": self.id, "title": self.title, "preset": self.preset, "template": self.template, "audio": self.audio,
            "seconds": self.seconds, "jobs": self.jobs,
            "clips": [{**c.model_dump(mode="json"), "at": start, "seconds": c.seconds, "tracked": bool(c.track)}
                      for start, c in zip(self.starts(), self.clips)],
        }


def timeline_from_cut(cut, *, job_id: str, source_path: str, preset: str = "reels", template: str | None = None,
                      width: int | None = None, height: int | None = None) -> Timeline:
    """A cut proposal (one window, or several parts of the same point) becomes an editable timeline."""
    parts = getattr(cut, "parts", None) or [(cut.in_seconds, cut.out_seconds)]
    clips = []
    for n, (a, b) in enumerate(parts, start=1):
        offset = a - cut.in_seconds
        clips.append(Clip(
            job_id=job_id, source_path=source_path, in_seconds=round(a, 3), out_seconds=round(b, 3),
            source_width=width, source_height=height, lower_third=cut.lower_third if n == 1 else None,
            captions=[c.model_copy(update={"start": max(0.0, c.start - offset), "end": c.end - offset})
                      for c in cut.captions if c.end > offset and c.start < offset + (b - a)],
            label=f"part {n}" if len(parts) > 1 else (cut.title or "cut"),
            note=cut.reason or "", sentences=list(getattr(cut, "sentence_ids", []) or []),
        ))
    return Timeline(title=cut.title, preset=preset, template=template, audio="source", clips=clips,
                    source={"kind": "cut", "id": cut.id, "job_id": job_id})
