"""Blog Agent: brief (+ optional anchoring video job) -> evidence pack -> grounded draft."""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from agents.blog_agent.schemas import BlogDraftV1, Citation
from services.ai_gateway.base import AIProvider, RawModelOutput
from services.block_engine.engine import PROMPTS_DIR, _fence_strip, load_prompt
from services.block_engine.schemas import provider_json_schema
from services.retrieval.retriever import EvidencePack, Retriever

log = logging.getLogger(__name__)
MIN_CITATIONS = 3
_MARKER = re.compile(r"\s*\[id=[^\]]+\]")


def strip_citation_markers(markdown: str) -> str:
    """Remove inline [id=...] markers (kept in the stored draft for review) for preview/publishing."""
    return re.sub(r"[ \t]+\n", "\n", _MARKER.sub("", markdown or ""))


def inline_citation_ids(markdown: str) -> list[str]:
    ids: list[str] = []
    for m in re.finditer(r"\[id=([^\]]+)\]", markdown or ""):
        for part in m.group(1).split(","):
            part = part.strip().removeprefix("id=").strip()
            if part and part not in ids:
                ids.append(part)
    return ids


class DraftValidationError(RuntimeError):
    pass


@dataclass
class BlogAgentResult:
    draft: BlogDraftV1
    evidence: EvidencePack
    model: str
    prompt_version: str
    usage: dict[str, int | None]
    seconds: float
    warnings: list[str] = field(default_factory=list)
    repaired: bool = False


class BlogAgent:
    name = "blog"

    def __init__(
        self,
        provider: AIProvider,
        retriever: Retriever,
        *,
        prompt_version: str = "blog_v1",
        style_guide_path: Path | None = None,
        institution_context: str = "",
        model: str | None = None,
        thinking_level: str | None = None,
    ):
        self.provider = provider
        self.retriever = retriever
        self.prompt_version = prompt_version
        self.institution_context = institution_context
        self.model = model
        self.thinking_level = thinking_level
        self.system_template, self.user_template = load_prompt_from(PROMPTS_DIR / "content-generation" / f"{prompt_version}.md")
        path = style_guide_path or PROMPTS_DIR / "content-generation" / "style_guide.md"
        self.style_guide = path.read_text(encoding="utf-8") if path.exists() else "(no style guide)"
        self.json_schema = provider_json_schema(BlogDraftV1)

    def draft(
        self,
        brief: str,
        *,
        job_id: str | None = None,
        extra_queries: list[str] | None = None,
        formats: list[str] | None = None,
        target_words: int = 1000,
    ) -> BlogAgentResult:
        started = time.monotonic()
        queries = [brief] + [q for q in (extra_queries or []) if q]
        evidence = self.retriever.evidence_for(queries, job_id=job_id)
        if not evidence.blocks:
            raise DraftValidationError("no evidence found for this brief - analyse a relevant video first")

        fmt = {
            "institution_context": self.institution_context,
            "style_guide": self.style_guide,
            "brief": brief,
            "target_words": target_words,
            "formats": ", ".join(formats or ["linkedin", "instagram", "facebook"]),
            "evidence": evidence.render(),
        }
        raw = self.provider.generate_structured(
            self.system_template.format(**fmt), self.user_template.format(**fmt), self.json_schema,
            model=self.model, thinking_level=self.thinking_level,
        )
        draft, repaired, used = self._validate_or_repair(raw)
        warnings = self._ground(draft, evidence, target_words)
        if evidence.truncated:
            warnings.append("evidence was truncated to fit the prompt budget")
        return BlogAgentResult(
            draft=draft, evidence=evidence, model=used.model, prompt_version=self.prompt_version,
            usage=used.usage, seconds=round(time.monotonic() - started, 2), warnings=warnings, repaired=repaired,
        )

    # ------------------------------------------------------------------
    def _validate_or_repair(self, raw: RawModelOutput) -> tuple[BlogDraftV1, bool, RawModelOutput]:
        try:
            return self._parse(raw.text), False, raw
        except (ValidationError, json.JSONDecodeError) as first:
            log.warning("blog draft failed validation, asking provider to repair: %s", first)
            fixed = self.provider.repair_json(raw.text, str(first), self.json_schema)
            try:
                fixed.usage = {k: (raw.usage.get(k) or 0) + (fixed.usage.get(k) or 0) for k in raw.usage} or fixed.usage
                return self._parse(fixed.text), True, fixed
            except (ValidationError, json.JSONDecodeError) as second:
                raise DraftValidationError(f"invalid draft after repair: {second}") from second

    @staticmethod
    def _parse(text: str) -> BlogDraftV1:
        return BlogDraftV1.model_validate(json.loads(_fence_strip(text)))

    @staticmethod
    def _ground(draft: BlogDraftV1, evidence: EvidencePack, target_words: int) -> list[str]:
        """Drop citations that point outside the evidence; flag weak grounding and length drift."""
        w: list[str] = []
        ids = evidence.ids()
        listed = {c.block_id for c in draft.citations}
        for bid in inline_citation_ids(draft.body_markdown):
            if bid in ids and bid not in listed:
                draft.citations.append(Citation(block_id=bid, used_for="cited inline"))
                listed.add(bid)
        bad = [c for c in draft.citations if c.block_id not in ids]
        if bad:
            draft.citations = [c for c in draft.citations if c.block_id in ids]
            w.append(f"{len(bad)} citation(s) pointed outside the evidence and were dropped")
        if len(draft.citations) < MIN_CITATIONS:
            w.append(f"weakly grounded: only {len(draft.citations)} citation(s)")
        if draft.hero_block_id and draft.hero_block_id not in ids:
            draft.hero_block_id = None
            w.append("hero_block_id was not in the evidence and was cleared")
        words = len(re.findall(r"\w+", draft.body_markdown))
        if words < target_words * 0.5 or words > target_words * 1.8:
            w.append(f"length {words} words vs target {target_words}")
        if len(draft.meta_description) > 160:
            w.append("meta_description longer than 155 characters")
        if not re.search(r"^## ", draft.body_markdown, re.MULTILINE):
            w.append("no H2 subheadings in the body")
        if draft.evidence_gaps:
            w.append(f"{len(draft.evidence_gaps)} evidence gap(s) reported by the writer")
        return w


def load_prompt_from(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    sections = re.split(r"^## (\w+)\s*$", text, flags=re.MULTILINE)
    parts = {sections[i].strip(): sections[i + 1].strip() for i in range(1, len(sections) - 1, 2)}
    return parts["system"], parts["user"]
