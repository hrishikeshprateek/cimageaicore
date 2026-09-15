"""Cut selection: propose 2-3 short windows from a job's quote / key_moment / transcript / person blocks.

Rule-based first (deterministic, offline). `refine_with_ai()` optionally asks the AI Gateway
(prompts/video-composer/cuts_v1.md, structured JSON) to improve the proposals and falls back to
the rule-based list on any failure.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from services.block_engine.media import seconds_to_ts, ts_to_seconds
from services.block_engine.schemas import KeyMomentBlock, PersonBlock, QuoteBlock, TranscriptSegment, VideoAnalysisV1, provider_json_schema
from services.video_composer.captions import CaptionCue, cues_for_window, is_generic_speaker, sentence_spans
from services.video_composer.renderer import LowerThird

log = logging.getLogger(__name__)
PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts" / "video-composer"
WORDS_PER_SECOND = 2.4                 # conversational Hindi / English
_IMPORTANCE = {"high": 0.5, "medium": 0.3, "low": 0.1}


class CutProposal(BaseModel):
    id: str
    title: str
    in_seconds: float
    out_seconds: float
    in_ts: str
    out_ts: str
    duration: float
    source: Literal["quote", "key_moment", "transcript", "ai"]
    score: float
    reason: str
    hook_line: str | None = None
    speaker: str | None = None
    lower_third: LowerThird | None = None
    captions: list[CaptionCue] = Field(default_factory=list)


class CutLimits(BaseModel):
    min_seconds: float = 8.0
    max_seconds: float = 60.0
    target_seconds: float = 30.0
    max_cuts: int = 3
    max_chars_per_line: int = 34
    max_lines: int = 2
    caption_min_seconds: float = 0.8


def _fmt(t: float) -> str:
    base = seconds_to_ts(int(t))
    frac = t - int(t)
    return f"{base}.{int(round(frac * 10)) % 10}" if frac >= 0.05 else base


def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9ऀ-ॿ]+", " ", s.lower()).strip()


def _segment_at(segments: list[TranscriptSegment], t: float) -> tuple[float, float, TranscriptSegment] | None:
    best = None
    for seg in segments:
        s, e = ts_to_seconds(seg.start_time), ts_to_seconds(seg.end_time)
        if s is None or e is None:
            continue
        if s <= t <= max(e, s + 1):
            return float(s), float(max(e, s + 1)), seg
        if best is None or abs(s - t) < abs(best[0] - t):
            best = (float(s), float(max(e, s + 1)), seg)
    return best


def _snap_in(seg: TranscriptSegment | None, t: float, *, default_lead: float = 4.0, max_lead: float = 12.0) -> float:
    """Start at the (estimated) beginning of the sentence that contains `t`, if that is not too far back."""
    starts = [s for s, _, _ in sentence_spans(seg)] if seg else []
    before = [s for s in starts if s <= t + 0.5]
    if before and t - max(before) <= max_lead:
        return max(before)
    return t - default_lead


def _snap_out(seg: TranscriptSegment | None, t_end: float, *, max_tail: float = 8.0) -> float:
    """End at the (estimated) end of the sentence that contains `t_end`, if that is not too far ahead."""
    ends = [e for _, e, _ in sentence_spans(seg)] if seg else []
    after = [e for e in ends if e >= t_end - 0.5]
    if after and min(after) - t_end <= max_tail:
        return min(after)
    return t_end


def _clamp_window(t_in: float, t_out: float, duration: float | None, lim: CutLimits, *, must_include: tuple[float, float] | None = None) -> tuple[float, float]:
    """Enforce min/max length and the video bounds while keeping `must_include` inside if possible."""
    if duration is not None:
        t_in, t_out = max(0.0, min(t_in, duration)), max(0.0, min(t_out, duration))
    if t_out - t_in < lim.min_seconds:
        t_out = t_in + lim.min_seconds
        if duration is not None and t_out > duration:
            t_out = duration
            t_in = max(0.0, t_out - lim.min_seconds)
    if t_out - t_in > lim.max_seconds:
        if must_include:
            a, b = must_include
            t_in = max(t_in, min(a - 3.0, b - lim.max_seconds + 1.0))
        t_out = t_in + lim.max_seconds
    return round(t_in, 2), round(t_out, 2)


def _person_for(speaker: str | None, people: list[PersonBlock], window: tuple[float, float]) -> LowerThird | None:
    """Lower-third from the person blocks: by speaker name first, then whoever is on screen inside the window."""
    if speaker and not is_generic_speaker(speaker):
        key = _norm_name(speaker)
        for p in people:
            n = _norm_name(p.name)
            if n and (n == key or n in key or key in n):
                return LowerThird(name=p.name, role=p.role)
        return LowerThird(name=speaker, role=None)
    a, b = window
    candidates = []
    for p in people:
        if is_generic_speaker(p.name) or p.identified_by == "unnamed":
            continue
        stamps = [ts_to_seconds(t) for t in p.timestamps]
        if any(s is not None and a - 2 <= s <= b + 2 for s in stamps):
            candidates.append((p.confidence, p))
    if candidates:
        p = max(candidates, key=lambda c: c[0])[1]
        return LowerThird(name=p.name, role=p.role)
    return None


def _overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    inter = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    shorter = max(0.1, min(a[1] - a[0], b[1] - b[0]))
    return inter / shorter


def _finish(pid: str, title: str, t_in: float, t_out: float, source: str, score: float, reason: str, *, hook: str | None, speaker: str | None,
            analysis: VideoAnalysisV1, lim: CutLimits) -> CutProposal:
    lt = _person_for(speaker, analysis.people, (t_in, t_out))
    cues = cues_for_window(analysis.transcript, t_in, t_out, max_chars_per_line=lim.max_chars_per_line, max_lines=lim.max_lines, min_seconds=lim.caption_min_seconds)
    return CutProposal(
        id=pid, title=title[:90], in_seconds=t_in, out_seconds=t_out, in_ts=_fmt(t_in), out_ts=_fmt(t_out), duration=round(t_out - t_in, 2),
        source=source, score=round(score, 3), reason=reason, hook_line=hook, speaker=speaker, lower_third=lt, captions=cues,
    )


def propose_cuts(analysis: VideoAnalysisV1, duration: float | None, lim: CutLimits | None = None) -> list[CutProposal]:
    lim = lim or CutLimits()
    segs = analysis.transcript
    proposals: list[CutProposal] = []

    def moments_in(a: float, b: float) -> list[KeyMomentBlock]:
        return [k for k in analysis.key_moments if (ts_to_seconds(k.timestamp) or -1) >= a and (ts_to_seconds(k.timestamp) or -1) <= b]

    # ---- quotes: the strongest testimonial material
    for i, q in enumerate(analysis.quotes):
        t = ts_to_seconds(q.timestamp)
        if t is None:
            continue
        if duration is not None and t > duration + 2:
            continue
        words = len(q.text.split())
        q_dur = max(2.0, words / WORDS_PER_SECOND + 0.8)
        seg = _segment_at(segs, t)
        inside = seg is not None and seg[0] <= t <= seg[1]
        t_in = _snap_in(seg[2] if inside else None, t)
        t_out = _snap_out(seg[2] if inside else None, t + q_dur + 1.0)
        t_in, t_out = _clamp_window(t_in, t_out, duration, lim, must_include=(t, t + q_dur))
        score = 1.0
        mom = moments_in(t_in, t_out)
        if mom:
            score += max(_IMPORTANCE[m.importance] for m in mom)
        if not is_generic_speaker(q.speaker):
            score += 0.2
        if lim.target_seconds * 0.5 <= t_out - t_in <= lim.target_seconds * 1.3:
            score += 0.2
        if 6 <= words <= 45:
            score += 0.1
        reason = f"quote by {q.speaker} at {q.timestamp}"
        if mom:
            reason += f"; key moment inside: {mom[0].description[:60]}"
        proposals.append(_finish(f"q{i}", q.text, t_in, t_out, "quote", score, reason, hook=q.text, speaker=q.speaker, analysis=analysis, lim=lim))

    # ---- key moments
    for i, k in enumerate(sorted(analysis.key_moments, key=lambda m: -_IMPORTANCE[m.importance])):
        t = ts_to_seconds(k.timestamp)
        if t is None or (duration is not None and t > duration + 2):
            continue
        seg = _segment_at(segs, t)
        inside = seg is not None and seg[0] <= t <= seg[1]
        t_in = _snap_in(seg[2] if inside else None, t, default_lead=3.0)
        t_out = _snap_out(seg[2] if inside else None, t + lim.target_seconds * 0.6)
        t_in, t_out = _clamp_window(t_in, t_out, duration, lim, must_include=(t, t + 3))
        speaker = next((s.speaker for s in segs if (ts_to_seconds(s.start_time) or 1e9) <= t <= (ts_to_seconds(s.end_time) or -1)), None)
        score = 0.6 + _IMPORTANCE[k.importance]
        proposals.append(_finish(f"k{i}", k.description, t_in, t_out, "key_moment", score, f"{k.importance} key moment at {k.timestamp}",
                                 hook=None, speaker=speaker, analysis=analysis, lim=lim))

    # ---- transcript fallback: the longest speaker turns
    if len(proposals) < 2:
        ranked = sorted(((ts_to_seconds(s.start_time), ts_to_seconds(s.end_time), s) for s in segs), key=lambda x: -((x[1] or 0) - (x[0] or 0)))
        for i, (s0, e0, seg) in enumerate(ranked[:3]):
            if s0 is None or e0 is None:
                continue
            t_in, t_out = _clamp_window(float(s0), min(float(e0), s0 + lim.target_seconds), duration, lim)
            proposals.append(_finish(f"t{i}", seg.text, t_in, t_out, "transcript", 0.3, f"speaker turn by {seg.speaker} at {seg.start_time}",
                                     hook=None, speaker=seg.speaker, analysis=analysis, lim=lim))
    if not proposals and duration:
        t_in, t_out = _clamp_window(0.0, min(duration, lim.target_seconds), duration, lim)
        proposals.append(_finish("t0", "Opening", t_in, t_out, "transcript", 0.1, "no quotes, key moments or transcript - opening of the video",
                                 hook=None, speaker=None, analysis=analysis, lim=lim))

    # ---- dedupe overlapping windows, best first
    proposals.sort(key=lambda p: (-p.score, p.in_seconds))
    kept: list[CutProposal] = []
    for p in proposals:
        if all(_overlap((p.in_seconds, p.out_seconds), (k.in_seconds, k.out_seconds)) < 0.5 for k in kept):
            kept.append(p)
        if len(kept) >= lim.max_cuts:
            break
    return kept


# --------------------------------------------------------------------------------------------
# optional AI refinement
# --------------------------------------------------------------------------------------------

class AICut(BaseModel):
    in_time: str = Field(description="HH:MM:SS start of the cut inside the source video")
    out_time: str = Field(description="HH:MM:SS end of the cut")
    title: str = Field(description="Short working title for the short, max 80 characters")
    hook_line: str | None = Field(description="The single strongest sentence spoken inside the cut, verbatim, or null")
    reason: str = Field(description="Why this window works as a standalone short")
    lower_third_name: str | None = Field(description="Name of the person speaking, exactly as it appears in the people blocks, or null")
    lower_third_role: str | None = Field(description="Their role/designation as given in the people blocks, or null")


class AICutsV1(BaseModel):
    cuts: list[AICut]


def load_prompt(version: str) -> tuple[str, str]:
    """prompts/video-composer/<version>.md -> (system, user). Placeholders are {name}; JSON braces are safe (no str.format)."""
    text = (PROMPTS_DIR / f"{version}.md").read_text(encoding="utf-8")
    sections = re.split(r"^## (\w+)\s*$", text, flags=re.MULTILINE)
    parts = {sections[i].strip(): sections[i + 1].strip() for i in range(1, len(sections) - 1, 2)}
    return parts["system"], parts["user"]


def _fill(template: str, values: dict[str, str]) -> str:
    for k, v in values.items():
        template = template.replace("{" + k + "}", v)
    return template


def _blocks_digest(analysis: VideoAnalysisV1, duration: float | None) -> str:
    def row(**kw):
        return json.dumps(kw, ensure_ascii=False)

    lines = [f"video: {analysis.video.title} | duration: {seconds_to_ts(duration) if duration else analysis.video.observed_duration or 'unknown'} | language: {analysis.video.language}"]
    lines.append("people:")
    lines += ["  " + row(name=p.name, role=p.role, timestamps=p.timestamps, identified_by=p.identified_by) for p in analysis.people]
    lines.append("quotes:")
    lines += ["  " + row(speaker=q.speaker, timestamp=q.timestamp, text=q.text) for q in analysis.quotes]
    lines.append("key_moments:")
    lines += ["  " + row(timestamp=k.timestamp, importance=k.importance, description=k.description) for k in analysis.key_moments]
    lines.append("transcript:")
    lines += ["  " + row(start=s.start_time, end=s.end_time, speaker=s.speaker, text=s.text) for s in analysis.transcript]
    return "\n".join(lines)


def refine_with_ai(provider, analysis: VideoAnalysisV1, duration: float | None, rule_based: list[CutProposal], *, lim: CutLimits | None = None,
                   prompt_version: str = "cuts_v1", institution_context: str = "") -> tuple[list[CutProposal], str | None]:
    """Ask the gateway for better windows. Returns (cuts, warning). Never raises: falls back to `rule_based`."""
    from services.ai_gateway.base import ProviderError

    lim = lim or CutLimits()
    try:
        system, user = load_prompt(prompt_version)
        values = {
            "institution_context": institution_context,
            "min_seconds": str(int(lim.min_seconds)), "max_seconds": str(int(lim.max_seconds)), "target_seconds": str(int(lim.target_seconds)),
            "max_cuts": str(lim.max_cuts),
            "blocks": _blocks_digest(analysis, duration),
            "rule_based": "\n".join(f"- {p.in_ts} -> {p.out_ts} ({p.duration:.0f}s) {p.source}: {p.title}" for p in rule_based) or "- none",
        }
        raw = provider.generate_structured(_fill(system, values), _fill(user, values), provider_json_schema(AICutsV1))
        text = raw.text.strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
        parsed = AICutsV1.model_validate(json.loads(text))
    except (ProviderError, ValidationError, json.JSONDecodeError, FileNotFoundError, KeyError, AttributeError, TypeError) as exc:
        log.warning("AI cut refinement unavailable, keeping rule-based cuts: %s", exc)
        return rule_based, f"AI refinement unavailable ({type(exc).__name__}); rule-based cuts shown"

    out: list[CutProposal] = []
    for i, c in enumerate(parsed.cuts[: lim.max_cuts]):
        a, b = ts_to_seconds(c.in_time), ts_to_seconds(c.out_time)
        if a is None or b is None or b <= a:
            continue
        a, b = _clamp_window(float(a), float(b), duration, lim)
        speaker = c.lower_third_name
        p = _finish(f"ai{i}", c.title, a, b, "ai", 2.0 - i * 0.1, c.reason, hook=c.hook_line, speaker=speaker, analysis=analysis, lim=lim)
        if c.lower_third_name:
            p.lower_third = LowerThird(name=c.lower_third_name, role=c.lower_third_role)
        out.append(p)
    if not out:
        return rule_based, "AI returned no usable cuts; rule-based cuts shown"
    return out, None
