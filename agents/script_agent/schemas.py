"""What the script writer must return: a timed, shootable video script grounded in the knowledge base."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Language = Literal["hi", "en", "hinglish"]
LANGUAGE_LABEL = {"hi": "Hindi (Devanagari)", "en": "English", "hinglish": "Hinglish (Hindi in Roman script)"}


class Scene(BaseModel):
    n: int = Field(ge=1, description="scene number, starting at 1")
    seconds: float = Field(gt=0, le=600, description="how long this scene is on screen")
    visual: str = Field(min_length=3, description="what the viewer sees - shot, framing, action")
    voiceover: str = Field(default="", description="the words spoken over this scene, in the chosen language")
    on_screen_text: str | None = Field(default=None, description="text burnt on screen; keep it short")
    b_roll_block_id: str | None = Field(default=None, description="evidence block id whose video moment this scene reuses (the footage exists)")
    b_roll_image_id: str | None = Field(default=None, description="still / library photo id to show instead of (or over) the footage")
    evidence_ids: list[str] = Field(default_factory=list, description="block ids backing every claim spoken in this scene")


class ScriptV1(BaseModel):
    title: str = Field(min_length=3, max_length=140, description="working title of the video")
    language: Language = "hi"
    hook: str = Field(min_length=3, description="the first line, spoken in the first ~3 seconds")
    scenes: list[Scene] = Field(min_length=1)
    cta: str = Field(default="", description="what the viewer should do at the end")
    caption: str = Field(default="", description="caption for the post")
    hashtags: list[str] = Field(default_factory=list)
    thumbnail_idea: str | None = None
    music_mood: str | None = None
    shot_list: list[str] = Field(default_factory=list, description="shots that must still be filmed because no footage exists")
    evidence_gaps: list[str] = Field(default_factory=list, description="claims the brief asked for that the library cannot back")
