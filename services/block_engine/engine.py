"""Block Engine: VideoSource -> AI provider -> validated knowledge blocks."""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from pydantic import ValidationError

from services.ai_gateway.base import AIProvider, ExtraPass, RawModelOutput, StageCallback, VideoAnalysisRequest, sum_usage
from services.block_engine.media import ts_to_seconds
from services.block_engine.schemas import AnalysisResult, PeoplePassV1, PersonBlock, UsageInfo, VideoAnalysisV1, provider_json_schema
from services.block_engine.sources import VideoSource

log = logging.getLogger(__name__)
PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


class BlockValidationError(RuntimeError):
    """The provider's output could not be turned into valid blocks, even after repair."""


def load_prompt(version: str) -> tuple[str, str]:
    """Read prompts/video-analysis/<version>.md -> (system, user) templates."""
    text = (PROMPTS_DIR / "video-analysis" / f"{version}.md").read_text(encoding="utf-8")
    sections = re.split(r"^## (\w+)\s*$", text, flags=re.MULTILINE)
    parts = {sections[i].strip(): sections[i + 1].strip() for i in range(1, len(sections) - 1, 2)}
    return parts["system"], parts["user"]


def load_known_people(path: Path | None) -> str:
    """prompts/known_people.txt -> bullet list for the prompt ('none listed' when absent/empty)."""
    if path is None or not path.exists():
        return "- none listed"
    names = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]
    return "\n".join(f"- {n}" for n in names) or "- none listed"


def _fence_strip(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t


class BlockEngine:
    def __init__(
        self,
        provider: AIProvider,
        *,
        prompt_version: str = "v1",
        institution_context: str = "",
        known_people_file: Path | None = None,
        retry_on_coarse_transcript: bool = True,
        people_pass_version: str | None = "people_v1",
    ):
        self.provider = provider
        self.retry_on_coarse_transcript = retry_on_coarse_transcript
        self.prompt_version = prompt_version
        self.institution_context = institution_context
        self.known_people = load_known_people(known_people_file)
        self.system_template, self.user_template = load_prompt(prompt_version)
        self.json_schema = provider_json_schema(VideoAnalysisV1)
        self.people_pass_version = people_pass_version
        if people_pass_version:
            self.people_system, self.people_user = load_prompt(people_pass_version)
            self.people_schema = provider_json_schema(PeoplePassV1)

    def analyze(self, job_id: str, source: VideoSource, on_stage: StageCallback) -> AnalysisResult:
        started = time.monotonic()
        fmt = {"institution_context": self.institution_context, "known_people": self.known_people, "source_name": source.info.name}
        request = VideoAnalysisRequest(
            video=source.to_input(),
            system_instruction=self.system_template.format(**fmt),  # unused keys are ignored
            prompt=self.user_template.format(**fmt),
            json_schema=self.json_schema,
            extra_passes=[ExtraPass("people", self.people_system.format(**fmt), self.people_user.format(**fmt), self.people_schema)]
            if self.people_pass_version else [],
        )
        raw = self.provider.analyze_video(request, on_stage)
        analysis, repaired, used = self._validate_or_repair(raw)
        analysis = self._merge_people(analysis, raw)
        if self._transcript_too_coarse(analysis, source) and self.retry_on_coarse_transcript:
            # Flash-Lite sometimes collapses the transcript into one segment; one re-run usually fixes it.
            on_stage("ANALYZING", {"retry": "transcript too coarse", "segments": len(analysis.transcript)})
            raw2 = self.provider.analyze_video(request, on_stage)
            analysis2, repaired2, used2 = self._validate_or_repair(raw2)
            analysis2 = self._merge_people(analysis2, raw2)
            used2.usage = {k: (used.usage.get(k) or 0) + (used2.usage.get(k) or 0) for k in used.usage}
            if len(analysis2.transcript) > len(analysis.transcript):
                analysis, repaired, used = analysis2, repaired or repaired2, used2
            else:
                used.usage = used2.usage
        on_stage("BLOCKS_PARTIAL", {"repaired": repaired, "counts": analysis.block_counts(), "note": "validated, persisting"})

        return AnalysisResult(
            job_id=job_id,
            source=source.info,
            provider=self.provider.name,
            model=used.model,
            prompt_version=self.prompt_version,
            processing_seconds=round(time.monotonic() - started, 2),
            usage=UsageInfo(**{k: v for k, v in used.usage.items() if k in UsageInfo.model_fields}),
            repaired=repaired,
            warnings=self._warnings(analysis, source),
            block_counts=analysis.block_counts(),
            analysis=analysis,
        )

    # ------------------------------------------------------------------
    def _merge_people(self, analysis: VideoAnalysisV1, raw: RawModelOutput) -> VideoAnalysisV1:
        """People from the focused pass win; main-pass people not found there are kept."""
        extra = raw.extras.get("people")
        if extra is None:
            return analysis
        try:
            found = PeoplePassV1.model_validate(json.loads(_fence_strip(extra.text))).people
        except (ValidationError, json.JSONDecodeError) as exc:
            log.warning("people pass returned invalid JSON, keeping main-pass people: %s", exc)
            return analysis
        seen = {p.name.strip().lower() for p in found}
        merged: list[PersonBlock] = list(found) + [p for p in analysis.people if p.name.strip().lower() not in seen]
        raw.usage = sum_usage(raw, extra)
        return analysis.model_copy(update={"people": merged})

    def _validate_or_repair(self, raw: RawModelOutput) -> tuple[VideoAnalysisV1, bool, RawModelOutput]:
        try:
            return self._parse(raw.text), False, raw
        except (ValidationError, json.JSONDecodeError) as first_err:
            log.warning("provider output failed validation, asking provider to repair: %s", first_err)
            fixed = self.provider.repair_json(raw.text, str(first_err), self.json_schema)
            try:
                merged_usage = {k: (raw.usage.get(k) or 0) + (fixed.usage.get(k) or 0) for k in raw.usage}
                fixed.usage = merged_usage or fixed.usage
                return self._parse(fixed.text), True, fixed
            except (ValidationError, json.JSONDecodeError) as second_err:
                raise BlockValidationError(f"invalid blocks after repair: {second_err}") from second_err

    @staticmethod
    def _parse(text: str) -> VideoAnalysisV1:
        return VideoAnalysisV1.model_validate(json.loads(_fence_strip(text)))

    @staticmethod
    def _transcript_too_coarse(a: VideoAnalysisV1, source: VideoSource) -> bool:
        """Fewer than one segment per 60 s of a video longer than 45 s (segments are asked at 30-60 s)."""
        dur = source.info.duration_seconds
        return bool(dur and dur > 45 and a.transcript and len(a.transcript) < max(2, dur / 60))

    @staticmethod
    def _warnings(a: VideoAnalysisV1, source: VideoSource) -> list[str]:
        w: list[str] = []
        if not a.transcript:
            w.append("transcript is empty")
        if not a.events:
            w.append("no event detected")
        dur = source.info.duration_seconds
        if BlockEngine._transcript_too_coarse(a, source):
            w.append(f"transcript is coarse: {len(a.transcript)} segment(s) for {dur:.0f}s of video")
        if dur:
            stamps = [s.start_time for s in a.transcript] + [k.timestamp for k in a.key_moments] + [q.timestamp for q in a.quotes]
            beyond = [t for t in stamps if (ts_to_seconds(t) or 0) > dur + 5]
            if beyond:
                w.append(f"{len(beyond)} timestamp(s) exceed the video duration ({dur:.0f}s), e.g. {beyond[0]}")
        return w
