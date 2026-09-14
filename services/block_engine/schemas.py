"""Knowledge-block schemas, version 1.

`VideoAnalysisV1` is exactly what the AI provider must return (strict JSON).
`AnalysisResult` wraps it with provenance (source, provider, model, usage).
`Block` is the flattened row shape that will map to the `knowledge_blocks`
table in Phase 3 (PostgreSQL + pgvector).
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.1"  # 1.1: PersonBlock.identified_by (optional, so 1.0 results still validate)

Timestamp = str  # "HH:MM:SS" position inside the video


class VideoBlock(BaseModel):
    title: str = Field(description="A concise, human-readable title for the video.")
    video_type: Literal["event", "speech", "interview", "lecture", "promotional", "news", "vlog", "other"]
    language: str = Field(description="Primary spoken language(s), e.g. 'en', 'hi', 'hi-en' for mixed Hindi/English.")
    description: str = Field(description="One paragraph describing what the video is and what happens in it.")
    observed_duration: Timestamp | None = Field(description="Approximate end timestamp observed in the video as HH:MM:SS, or null.")


class EventBlock(BaseModel):
    name: str
    date: str | None = Field(description="ISO date YYYY-MM-DD if stated or visible on screen, otherwise null. Never guess.")
    venue: str | None
    organizer: str | None
    description: str
    confidence: float = Field(ge=0, le=1)


class PersonBlock(BaseModel):
    name: str = Field(description="Name as spoken or shown on screen, or the recognised person's name.")
    role: str | None = Field(description="Designation/role if known, e.g. 'Director, CIMAGE' or 'Guest speaker'.")
    context: str = Field(description="What this person does in the video.")
    timestamps: list[Timestamp] = Field(description="Timestamps (HH:MM:SS) where this person appears or speaks.")
    identified_by: Literal["on_screen", "spoken", "recognised", "unnamed"] | None = Field(
        default=None,
        description="How the name was established: shown on screen, spoken aloud, recognised without a name in the video, or unnamed (Speaker N / role only).",
    )
    confidence: float = Field(ge=0, le=1)


class TranscriptSegment(BaseModel):
    speaker: str = Field(description="Speaker name if identifiable, otherwise 'Speaker 1', 'Speaker 2', 'Audience', etc.")
    start_time: Timestamp
    end_time: Timestamp
    text: str = Field(description="What was said, in the language spoken (Devanagari for Hindi). One speaker turn or ~30-60s of speech per segment.")
    language: str | None = Field(description="'en', 'hi', or 'hi-en' for this segment.")


class TopicBlock(BaseModel):
    name: str
    evidence: str = Field(description="Short justification citing what was said or shown.")
    timestamps: list[Timestamp]
    confidence: float = Field(ge=0, le=1)


class KeyMomentBlock(BaseModel):
    timestamp: Timestamp
    description: str
    importance: Literal["low", "medium", "high"]


class QuoteBlock(BaseModel):
    speaker: str
    text: str = Field(description="Verbatim quote, in the language spoken.")
    timestamp: Timestamp
    source_reference: str = Field(description="Where this comes from, e.g. 'speech at 00:12:31' or 'on-screen text'.")


class MediaBlock(BaseModel):
    timestamp: Timestamp
    description: str = Field(description="Visual description of this frame/scene (people, setting, banners, on-screen text).")
    suitable_for: list[Literal["thumbnail", "blog_hero", "social_post", "press", "archive"]]


class SummaryBlock(BaseModel):
    short_summary: str = Field(description="1-2 sentences.")
    detailed_summary: str = Field(description="1-3 paragraphs.")
    key_points: list[str]


ContentFormat = Literal[
    "blog", "news_article", "linkedin", "instagram", "facebook", "x", "youtube_description", "newsletter", "press_release"
]


class ContentOpportunityBlock(BaseModel):
    title: str = Field(description="Working headline for the content piece.")
    reason: str = Field(description="Why this video supports this piece of content.")
    suggested_formats: list[ContentFormat]
    audience: str = Field(description="Who this content is for, e.g. prospective students, parents, industry partners, alumni.")
    confidence: float = Field(ge=0, le=1)


class VideoAnalysisV1(BaseModel):
    """Strict structure the AI provider must return."""

    video: VideoBlock
    events: list[EventBlock]
    people: list[PersonBlock]
    transcript: list[TranscriptSegment]
    topics: list[TopicBlock]
    key_moments: list[KeyMomentBlock]
    quotes: list[QuoteBlock]
    media: list[MediaBlock]
    summary: SummaryBlock
    content_opportunities: list[ContentOpportunityBlock]

    def block_counts(self) -> dict[str, int]:
        return {
            "events": len(self.events),
            "people": len(self.people),
            "transcript": len(self.transcript),
            "topics": len(self.topics),
            "key_moments": len(self.key_moments),
            "quotes": len(self.quotes),
            "media": len(self.media),
            "content_opportunities": len(self.content_opportunities),
        }


class PeoplePassV1(BaseModel):
    """Output of the focused people pass."""

    people: list[PersonBlock]


# --------------------------------------------------------------------------
# Provenance wrapper (what we persist)
# --------------------------------------------------------------------------

class SourceInfo(BaseModel):
    kind: Literal["upload", "nas_file", "online"]
    name: str
    path: str | None = None
    url: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    duration_seconds: float | None = None


class UsageInfo(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    thought_tokens: int | None = None
    total_tokens: int | None = None


class AnalysisResult(BaseModel):
    schema_version: str = SCHEMA_VERSION
    job_id: str
    source: SourceInfo
    provider: str
    model: str
    prompt_version: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    processing_seconds: float
    usage: UsageInfo = Field(default_factory=UsageInfo)
    repaired: bool = False
    warnings: list[str] = Field(default_factory=list)
    block_counts: dict[str, int] = Field(default_factory=dict)
    analysis: VideoAnalysisV1


class Block(BaseModel):
    """Flattened knowledge block - the row shape for the future knowledge_blocks table."""

    block_id: str
    block_type: str
    job_id: str
    source_name: str
    timestamp: str | None = None
    payload: dict[str, Any]
    text: str = Field(description="Searchable text representation (what gets embedded in Phase 3).")


class SearchHit(Block):
    media_id: str | None = None
    rank: float = 0.0


def flatten_blocks(result: AnalysisResult) -> list[Block]:
    a = result.analysis
    blocks: list[Block] = []
    src = result.source.name

    def add(kind: str, idx: int, payload: dict[str, Any], text: str, ts: str | None = None) -> None:
        blocks.append(
            Block(
                block_id=f"{result.job_id}:{kind}:{idx}",
                block_type=kind,
                job_id=result.job_id,
                source_name=src,
                timestamp=ts,
                payload=payload,
                text=text.strip(),
            )
        )

    v = a.video
    add("video", 0, v.model_dump(), f"{v.title}. {v.description}")
    s = a.summary
    add("summary", 0, s.model_dump(), f"{s.short_summary}\n{s.detailed_summary}\n" + "\n".join(s.key_points))
    for i, e in enumerate(a.events):
        add("event", i, e.model_dump(), f"{e.name} ({e.date or 'date unknown'}) at {e.venue or 'unknown venue'}. {e.description}")
    for i, p in enumerate(a.people):
        add("person", i, p.model_dump(), f"{p.name}, {p.role or 'role unknown'}. {p.context}", p.timestamps[0] if p.timestamps else None)
    for i, t in enumerate(a.transcript):
        add("transcript", i, t.model_dump(), f"{t.speaker}: {t.text}", t.start_time)
    for i, t in enumerate(a.topics):
        add("topic", i, t.model_dump(), f"{t.name}. {t.evidence}", t.timestamps[0] if t.timestamps else None)
    for i, k in enumerate(a.key_moments):
        add("key_moment", i, k.model_dump(), k.description, k.timestamp)
    for i, q in enumerate(a.quotes):
        add("quote", i, q.model_dump(), f"{q.speaker}: \"{q.text}\"", q.timestamp)
    for i, m in enumerate(a.media):
        add("media", i, m.model_dump(), m.description, m.timestamp)
    for i, c in enumerate(a.content_opportunities):
        add("content_opportunity", i, c.model_dump(), f"{c.title}. {c.reason}")
    return blocks


# --------------------------------------------------------------------------
# JSON schema for the provider
# --------------------------------------------------------------------------

_STRIP_KEYS = {"title", "default"}


def _inline_refs(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        if "$ref" in node:
            ref = node["$ref"].split("/")[-1]
            merged = copy.deepcopy(defs[ref])
            merged.update({k: v for k, v in node.items() if k != "$ref"})
            return _inline_refs(merged, defs)
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k in _STRIP_KEYS or k == "$defs":
                continue
            if k == "properties" and isinstance(v, dict):
                # property *names* are not schema keywords - never strip them
                out[k] = {name: _inline_refs(sub, defs) for name, sub in v.items()}
                continue
            out[k] = _inline_refs(v, defs)
        # {"anyOf": [{"type": X}, {"type": "null"}]}  ->  {"type": [X, "null"]}
        if "anyOf" in out:
            variants = out.pop("anyOf")
            non_null = [x for x in variants if x.get("type") != "null"]
            has_null = len(non_null) != len(variants)
            if len(non_null) == 1:
                base = non_null[0]
                if has_null and isinstance(base.get("type"), str):
                    base = {**base, "type": [base["type"], "null"]}
                out = {**base, **{k: v for k, v in out.items() if k not in base}}
            else:
                out["anyOf"] = variants
        return out
    if isinstance(node, list):
        return [_inline_refs(x, defs) for x in node]
    return node


def provider_json_schema(model: type[BaseModel] = VideoAnalysisV1) -> dict[str, Any]:
    """Pydantic JSON schema, flattened to the subset the Gemini API accepts
    (no $ref/$defs, no titles/defaults, nullable as a type list)."""
    raw = model.model_json_schema()
    defs = raw.get("$defs", {})
    return _inline_refs(raw, defs)
