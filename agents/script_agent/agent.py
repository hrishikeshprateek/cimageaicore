"""Script Agent: a spoken or typed idea -> evidence from our own videos -> a timed, shootable script.

Same shape as the blog agent, different product: instead of an article it returns scenes with a duration, a visual, a
voiceover line in the chosen language and - where the library has footage - the exact block / still the scene reuses.
The length is driven by the video length the user asked for: seconds x speaking rate = the word budget, checked afterwards.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import ValidationError

from agents.script_agent.schemas import LANGUAGE_LABEL, ScriptV1
from services import prompts
from services.ai_gateway.base import AIProvider, RawModelOutput
from services.block_engine.engine import _fence_strip
from services.block_engine.schemas import provider_json_schema
from services.retrieval.retriever import EvidencePack, Retriever

log = logging.getLogger(__name__)

# Voiceover pace at a natural reel/explainer delivery (words per second). Hindi carries more meaning per word than English.
WORDS_PER_SECOND = {"hi": 2.1, "hinglish": 2.3, "en": 2.5}

LANGUAGE_RULES = {
    "hi": ("Write every voiceover line in Devanagari script (क ख ग) - natural spoken Hindi, the way a Patna student actually talks, not textbook Hindi. "
           "This is not optional: a Hindi line typed in Roman letters is wrong here, that is what the Hinglish setting is for. "
           "Only the English words Hindi speakers use anyway (placement, campus, package, faculty, seminar) stay in Roman script inside the Devanagari line. "
           "on_screen_text stays short and may be English or Roman Hindi so it reads at a glance."),
    "hinglish": ("Write every voiceover line in Hinglish: Hindi sentences typed in Roman script, mixed with the English words students actually use. "
                 "Never Devanagari. Keep it casual and spoken, the way reels sound."),
    "en": "Write every voiceover line in clear, warm Indian English. Short sentences. No jargon, no marketing cliches.",
}

STYLES = {
    "viral_reel": ("Format: a vertical short (Instagram Reel / YouTube Short). It lives or dies on the first 3 seconds, so the hook must create "
                   "a question, a number or a contrast that stops the scroll - never a greeting and never the institution's name first. "
                   "Keep one idea per scene, cut every word that is not doing work, and land a clear CTA at the end."),
    "testimonial": ("Format: a student / parent testimonial edit. Build it around real verbatim quotes from the evidence - the voiceover mostly "
                    "introduces and frames what the speaker says. Name and role of every speaker must come from the evidence, never invented."),
    "campus_tour": ("Format: a walkthrough of the campus or a facility. Each scene is a place; the voiceover says what happens there and why it "
                    "matters to a student. Move in a sensible physical order."),
    "explainer": ("Format: an explainer for prospective students and parents. Answer one question properly: what, how it works, what it costs them "
                  "in effort, what they get. Calm and factual, no hype."),
    "announcement": ("Format: an announcement (admission, result, event). State the news in the first line, then the three things the viewer must "
                     "know (who, when, what to do), then the CTA."),
    "ad": ("Format: a paid ad. Problem the viewer feels, the specific proof we have, the offer, the CTA. Every claim must be backed by evidence; "
           "no superlatives that the evidence cannot support."),
}
DEFAULT_STYLE = "viral_reel"
SCENE_SECONDS = 6.5          # a short-form scene; long videos get proportionally longer scenes
MAX_SCENES = 40
EVIDENCE_BUDGET = {"max_blocks": 60, "max_chars": 20000}
_MARKER = re.compile(r"\[(?:id|img)=[^\]]*\]")
_DEVANAGARI = re.compile(r"[\u0900-\u097F]")


def _bare_id(value: str | None) -> str | None:
    """Models copy the marker they were shown ("[id=job:quote:1]") instead of the bare id - take either."""
    if not value:
        return None
    return value.strip().strip("[]").removeprefix("id=").removeprefix("img=").strip() or None


def scene_plan(seconds: int) -> tuple[int, float]:
    """How many scenes a video of this length wants, and how long each one runs."""
    per = SCENE_SECONDS if seconds <= 120 else min(20.0, SCENE_SECONDS + seconds / 60)
    n = max(2, min(MAX_SCENES, round(seconds / per)))
    return n, round(seconds / n, 1)


def word_budget(seconds: int, language: str) -> int:
    return max(12, round(seconds * WORDS_PER_SECOND.get(language, 2.2)))


def count_words(text: str) -> int:
    return len([w for w in re.split(r"\s+", (text or "").strip()) if w])


class ScriptValidationError(RuntimeError):
    pass


@dataclass
class ScriptAgentResult:
    script: ScriptV1
    evidence: EvidencePack
    model: str
    prompt_version: str
    usage: dict[str, int | None]
    seconds: float
    warnings: list[str] = field(default_factory=list)
    repaired: bool = False
    offered_images: list[Any] = field(default_factory=list)
    target_seconds: int = 45
    words: int = 0


class ScriptAgent:
    name = "script"

    def __init__(self, provider: AIProvider, retriever: Retriever, *, prompt_version: str = "script_v1",
                 institution_context: str = "", model: str | None = None, thinking_level: str | None = None):
        self.provider = provider
        self.retriever = retriever
        self.prompt_version = prompt_version
        self.institution_context = institution_context
        self.model = model
        self.thinking_level = thinking_level
        self.system_template, self.user_template = prompts.prompt("script", prompt_version)
        self.json_schema = provider_json_schema(ScriptV1)

    def reload(self, registry) -> None:
        """Prompt edited in the admin UI: pick the active version up without a restart."""
        self.prompt_version = registry.active("script")
        self.institution_context = registry.institution_context
        self.system_template, self.user_template = registry.prompt("script")

    # ------------------------------------------------------------------
    def write(
        self,
        idea: str,
        *,
        language: str = "hi",
        seconds: int = 45,
        style: str = DEFAULT_STYLE,
        job_id: str | None = None,
        extra_queries: list[str] | None = None,
        images_for: Callable[[EvidencePack], list[Any]] | None = None,
    ) -> ScriptAgentResult:
        started = time.monotonic()
        language = language if language in LANGUAGE_RULES else "hi"
        style = style if style in STYLES else DEFAULT_STYLE
        seconds = max(10, min(1800, int(seconds)))
        queries = [idea] + [q for q in (extra_queries or []) if q]
        evidence = self.retriever.evidence_for(queries, job_id=job_id, **EVIDENCE_BUDGET)
        if not evidence.blocks:
            raise ScriptValidationError("no evidence found for this idea - analyse a relevant video first, or say it differently")
        offered = list(images_for(evidence)) if images_for else []
        n_scenes, per_scene = scene_plan(seconds)
        words = word_budget(seconds, language)

        fmt = {
            "institution_context": self.institution_context,
            "idea": idea,
            "language": LANGUAGE_LABEL[language],
            "language_rules": LANGUAGE_RULES[language],
            "style_instructions": STYLES[style],
            "seconds": seconds,
            "target_words": words,
            "scene_count": n_scenes,
            "scene_seconds": per_scene,
            "evidence": evidence.render(),
            "images": "\n".join(im.offer_line() for im in offered) or "- (no stills available - describe the shot instead)",
        }
        raw = self.provider.generate_structured(
            self.system_template.format(**fmt), self.user_template.format(**fmt), self.json_schema,
            model=self.model, thinking_level=self.thinking_level,
        )
        script, repaired, used = self._validate_or_repair(raw)
        script.language = language
        warnings = self._ground(script, evidence, offered, seconds, words)
        if evidence.truncated:
            warnings.append("evidence was truncated to fit the prompt budget")
        spoken = sum(count_words(s.voiceover) for s in script.scenes)
        return ScriptAgentResult(
            script=script, evidence=evidence, model=used.model, prompt_version=self.prompt_version, usage=used.usage,
            seconds=round(time.monotonic() - started, 2), warnings=warnings, repaired=repaired, offered_images=offered,
            target_seconds=seconds, words=spoken,
        )

    # ------------------------------------------------------------------
    def _validate_or_repair(self, raw: RawModelOutput) -> tuple[ScriptV1, bool, RawModelOutput]:
        try:
            return self._parse(raw.text), False, raw
        except (ValidationError, json.JSONDecodeError) as first:
            log.warning("script failed validation, asking the provider to repair: %s", first)
            fixed = self.provider.repair_json(raw.text, str(first), self.json_schema)
            try:
                fixed.usage = {k: (raw.usage.get(k) or 0) + (fixed.usage.get(k) or 0) for k in raw.usage} or fixed.usage
                return self._parse(fixed.text), True, fixed
            except (ValidationError, json.JSONDecodeError) as second:
                raise ScriptValidationError(f"model did not return a usable script: {second}") from second

    @staticmethod
    def _parse(text: str) -> ScriptV1:
        return ScriptV1.model_validate(json.loads(_fence_strip(text)))

    def _ground(self, script: ScriptV1, evidence: EvidencePack, offered: list[Any], seconds: int, words: int) -> list[str]:
        """Nothing invented, nothing unshootable: ids must exist, and the script must fit the length that was asked for."""
        warnings: list[str] = []
        known_blocks, known_images = evidence.ids(), {im.id for im in offered}
        for s in script.scenes:
            s.voiceover = _MARKER.sub("", s.voiceover or "").strip()          # markers are for us, not for the presenter
            s.on_screen_text = _MARKER.sub("", s.on_screen_text or "").strip() or None
            ids = [_bare_id(i) for i in s.evidence_ids]
            s.evidence_ids = [i for i in ids if i in known_blocks]
            dropped = [i for i in ids if i and i not in known_blocks]
            if dropped:
                warnings.append(f"scene {s.n}: {', '.join(dropped[:3])} is not in the library - dropped")
            s.b_roll_block_id = _bare_id(s.b_roll_block_id)
            s.b_roll_image_id = _bare_id(s.b_roll_image_id)
            if s.b_roll_block_id and s.b_roll_block_id not in known_blocks:
                warnings.append(f"scene {s.n}: b-roll block {s.b_roll_block_id} is not in the evidence - shoot it instead")
                s.b_roll_block_id = None
            if s.b_roll_image_id and s.b_roll_image_id not in known_images:
                warnings.append(f"scene {s.n}: picture {s.b_roll_image_id} was not offered - removed")
                s.b_roll_image_id = None
        for i, s in enumerate(script.scenes, start=1):
            s.n = i
        planned = sum(s.seconds for s in script.scenes)
        if planned and abs(planned - seconds) > max(4, seconds * 0.2):
            warnings.append(f"the scenes add up to {planned:.0f}s, not the {seconds}s asked for")
        spoken = sum(count_words(s.voiceover) for s in script.scenes)
        if spoken and abs(spoken - words) > words * 0.3:
            warnings.append(f"{spoken} words of voiceover for a {seconds}s video (about {words} fit at a natural pace)")
        if count_words(script.hook) > 16:
            warnings.append("the hook is long for the first three seconds")
        if not any(s.evidence_ids for s in script.scenes):
            warnings.append("no scene cites the knowledge base - check every claim before shooting")
        if not any(s.b_roll_block_id or s.b_roll_image_id for s in script.scenes):
            warnings.append("no scene reuses existing footage - everything here has to be filmed")
        spoken_text = " ".join(s.voiceover for s in script.scenes)
        if script.language == "hi" and spoken_text and not _DEVANAGARI.search(spoken_text):
            warnings.append("asked for Hindi but the voiceover came back in Roman script - rewrite, or switch the script to Hinglish")
        if script.language == "hinglish" and _DEVANAGARI.search(spoken_text):
            warnings.append("asked for Hinglish (Roman) but parts came back in Devanagari")
        return warnings
